from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.services.history_orders import (
    build_display_registry,
    display_order_number,
    is_history_order_number,
)


router = APIRouter()
can_read = RoleChecker(["admin", "sales"])
can_operate = RoleChecker(["admin", "sales"])
admin_only = RoleChecker(["admin"])
SPECIAL_PROCESSES = {"无", "大做小", "双拼", "多拼"}


class RequisitionLinePayload(BaseModel):
    order_item_id: int
    inventory_deducted_qty: int = Field(default=0, ge=0)
    requisition_qty: int | None = Field(default=None, ge=0)
    cardboard_len: Decimal = Field(gt=0)
    cardboard_width: Decimal = Field(gt=0)
    special_process: str = "无"
    remark: str | None = None

    @field_validator("special_process")
    @classmethod
    def validate_process(cls, value: str) -> str:
        if value not in SPECIAL_PROCESSES:
            raise ValueError("特殊处理仅允许：无、大做小、双拼、多拼")
        return value


class RequisitionBatchCreate(BaseModel):
    supplier_name: str | None = None
    items: list[RequisitionLinePayload]

    @field_validator("items")
    @classmethod
    def validate_items(
        cls,
        value: list[RequisitionLinePayload],
    ) -> list[RequisitionLinePayload]:
        if not value:
            raise ValueError("至少选择一条待报料明细")
        ids = [item.order_item_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("同一订单明细不能重复报料")
        return value


class RequisitionEdit(BaseModel):
    inventory_deducted_qty: int = Field(ge=0)
    requisition_qty: int = Field(ge=0)
    cardboard_len: Decimal = Field(gt=0)
    cardboard_width: Decimal = Field(gt=0)
    special_process: str
    remark: str | None = None

    @field_validator("special_process")
    @classmethod
    def validate_process(cls, value: str) -> str:
        if value not in SPECIAL_PROCESSES:
            raise ValueError("特殊处理仅允许：无、大做小、双拼、多拼")
        return value


class SupplierSchedulePayload(BaseModel):
    supplier_delivery_time: datetime
    supplier_order_number: str | None = None


class CancelPayload(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("取消报料原因不能为空")
        return reason


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _suggested_dimensions(product: Product) -> tuple[Decimal | None, Decimal | None]:
    if (
        product.box_category != "normal"
        or product.length_mm is None
        or product.width_mm is None
        or product.height_mm is None
    ):
        return None, None
    cardboard_len = (product.length_mm + product.width_mm + Decimal("8")) * 2
    cardboard_width = product.width_mm + product.height_mm + Decimal("4")
    return cardboard_len, cardboard_width


def _next_number(db: Session, requisition_date: date) -> str:
    sequence = db.execute(
        text(
            """
            INSERT INTO requisition_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": requisition_date.isoformat()},
    ).scalar_one()
    if sequence > 999:
        raise HTTPException(status_code=409, detail="当日报料单流水号已超过999")
    return f"BL-{requisition_date:%Y%m%d}-{sequence:03d}"


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    entity_id: int,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource="Requisition",
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=entity_id,
            description=description,
        )
    )


def _item_or_404(db: Session, item_id: int) -> OrderItem:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    return item


def _item_response(item: OrderItem) -> dict:
    return {
        "item_id": item.id,
        "inventory_deducted_qty": item.inventory_deducted_qty,
        "requisition_qty": item.requisition_qty,
        "requisition_status": item.requisition_status,
        "special_process": item.special_process,
        "requisition_spec": item.requisition_spec,
        "cardboard_len": item.cardboard_len,
        "cardboard_width": item.cardboard_width,
        "requisition_date": item.requisition_date,
        "supplier_delivery_time": item.supplier_delivery_time,
        "supplier_order_number": item.supplier_order_number,
        "remark": item.requisition_remark,
    }


@router.get("/pending")
def pending_requisitions(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    rows = db.execute(
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            OrderItem.requisition_status == "未报料",
            OrderItem.material_status == "pending",
            Order.status != "cancelled",
        )
        .order_by(Order.delivery_date, Order.order_number, OrderItem.id)
    ).all()
    items = []
    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        suggested_len, suggested_width = _suggested_dimensions(product)
        items.append(
            {
                "item_id": item.id,
                "order_number": display_order_number(order, registry),
                "display_order_number": display_order_number(order, registry),
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_id": product.id,
                "product_code": (
                    item.snapshot_product_code or product.product_code
                ),
                "product_name": item.snapshot_product_name,
                "specification": item.snapshot_spec,
                "material": item.snapshot_material,
                "quantity": item.quantity,
                "delivery_date": order.delivery_date,
                "inventory_deducted_qty": item.inventory_deducted_qty,
                "requisition_qty": (
                    item.requisition_qty
                    if item.requisition_qty is not None
                    else max(item.quantity - item.inventory_deducted_qty, 0)
                ),
                "requisition_status": item.requisition_status,
                "special_process": item.special_process,
                "suggested_cardboard_len": (
                    item.cardboard_len or suggested_len
                ),
                "suggested_cardboard_width": (
                    item.cardboard_width or suggested_width
                ),
                # v0.19.2-B: 报料快照字段
                "layer_count": item.layer_count,
                "flute_type": item.flute_type,
                "material_id": item.material_id,
                "snapshot_supplier_name": item.snapshot_supplier_name,
                "snapshot_report_length_mm": item.snapshot_report_length_mm,
                "snapshot_report_width_mm": item.snapshot_report_width_mm,
                "snapshot_crease_type": item.snapshot_crease_type,
                "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
                "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
                "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
                "snapshot_report_notes": item.snapshot_report_notes,
            }
        )
    return {"items": items}


@router.get("/items")
def list_requisition_items(
    status_filter: str | None = Query(default=None, alias="status"),
    include_history: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    query = (
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .order_by(
            OrderItem.requisition_date.desc(),
            Order.delivery_date,
            OrderItem.id.desc(),
        )
    )
    rows = db.execute(query).all()
    items: list[dict] = []
    for item, order, customer, product in rows:
        is_history = is_history_order_number(order.order_number)
        if is_history and not include_history:
            continue
        effective_status = item.requisition_status
        if is_history and effective_status == "未报料":
            effective_status = "settled"
        if not is_history and effective_status == "未报料":
            continue
        if status_filter and effective_status != status_filter:
            continue
        items.append(
            {
                **_item_response(item),
                "requisition_status": effective_status,
                "order_number": display_order_number(order, registry),
                "display_order_number": display_order_number(order, registry),
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_id": product.id,
                "product_code": item.snapshot_product_code or product.product_code,
                "product_name": item.snapshot_product_name,
                "specification": item.snapshot_spec,
                "material": item.snapshot_material,
                "quantity": item.quantity,
                "delivery_date": order.delivery_date,
                "material_status": item.material_status,
            }
        )
    return {
        "items": items
    }


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def create_batch(
    payload: RequisitionBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    requisition_date = date.today()
    try:
        batch = Requisition(
            requisition_number=_next_number(db, requisition_date),
            requisition_date=requisition_date,
            supplier_name=(payload.supplier_name or "").strip() or None,
            status="已报料",
            created_by=user.id,
        )
        db.add(batch)
        db.flush()
        response_items = []
        for line in payload.items:
            row = db.execute(
                select(OrderItem, Product)
                .join(Product, Product.id == OrderItem.product_id)
                .where(OrderItem.id == line.order_item_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="订单明细不存在")
            item, product = row
            if item.material_status == "received":
                raise HTTPException(status_code=409, detail="已入库明细不能报料")
            if item.requisition_status != "未报料":
                raise HTTPException(status_code=409, detail="订单明细已经报料")
            if line.inventory_deducted_qty > item.quantity:
                raise HTTPException(status_code=400, detail="库存抵扣数不能超过订单数")
            requisition_qty = (
                line.requisition_qty
                if line.requisition_qty is not None
                else item.quantity - line.inventory_deducted_qty
            )
            if requisition_qty < 0:
                raise HTTPException(status_code=400, detail="实际报料数不能为负数")
            spec = f"{_plain(line.cardboard_len)}×{_plain(line.cardboard_width)}"
            item.inventory_deducted_qty = line.inventory_deducted_qty
            item.requisition_qty = requisition_qty
            item.requisition_status = "已报料"
            item.special_process = line.special_process
            item.cardboard_len = line.cardboard_len
            item.cardboard_width = line.cardboard_width
            item.requisition_spec = spec
            item.requisition_date = requisition_date
            item.requisition_remark = (line.remark or "").strip() or None
            batch_item = RequisitionItem(
                requisition_id=batch.id,
                order_item_id=item.id,
                inventory_deducted_qty=line.inventory_deducted_qty,
                requisition_qty=requisition_qty,
                cardboard_len=line.cardboard_len,
                cardboard_width=line.cardboard_width,
                special_process=line.special_process,
                material_snapshot=item.snapshot_material,
                product_code_snapshot=(
                    item.snapshot_product_code or product.product_code
                ),
                product_name_snapshot=item.snapshot_product_name,
                specification_snapshot=item.snapshot_spec,
                remark=item.requisition_remark,
                status="有效",
            )
            db.add(batch_item)
            response_items.append(_item_response(item))
        _audit(
            db,
            user=user,
            action="CREATE_REQUISITION",
            entity_id=batch.id,
            details={
                "requisition_number": batch.requisition_number,
                "item_ids": [line.order_item_id for line in payload.items],
            },
            description="生成采购报料单",
        )
        db.commit()
        return {
            "id": batch.id,
            "requisition_number": batch.requisition_number,
            "requisition_date": batch.requisition_date,
            "supplier_name": batch.supplier_name,
            "status": batch.status,
            "items": response_items,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/items/{item_id}")
def edit_requisition(
    item_id: int,
    payload: RequisitionEdit,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    item = _item_or_404(db, item_id)
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细禁止修改报料")
    if item.requisition_status == "未报料":
        raise HTTPException(status_code=409, detail="该明细尚未报料")
    if payload.inventory_deducted_qty > item.quantity:
        raise HTTPException(status_code=400, detail="库存抵扣数不能超过订单数")
    item.inventory_deducted_qty = payload.inventory_deducted_qty
    item.requisition_qty = payload.requisition_qty
    item.cardboard_len = payload.cardboard_len
    item.cardboard_width = payload.cardboard_width
    item.requisition_spec = (
        f"{_plain(payload.cardboard_len)}×{_plain(payload.cardboard_width)}"
    )
    item.special_process = payload.special_process
    item.requisition_remark = (payload.remark or "").strip() or None
    _audit(
        db,
        user=user,
        action="UPDATE_REQUISITION",
        entity_id=item.id,
        details=payload.model_dump(),
        description="修改报料信息",
    )
    db.commit()
    return _item_response(item)


@router.put("/items/{item_id}/supplier-schedule")
def supplier_schedule(
    item_id: int,
    payload: SupplierSchedulePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    item = _item_or_404(db, item_id)
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细禁止登记排单")
    if item.requisition_status not in {"已报料", "供应商已排单"}:
        raise HTTPException(status_code=409, detail="请先完成报料")
    item.requisition_status = "供应商已排单"
    item.supplier_delivery_time = payload.supplier_delivery_time
    item.supplier_order_number = (
        payload.supplier_order_number or ""
    ).strip() or None
    _audit(
        db,
        user=user,
        action="SUPPLIER_SCHEDULE",
        entity_id=item.id,
        details=payload.model_dump(),
        description="登记供应商排单回执",
    )
    db.commit()
    return _item_response(item)


@router.put("/items/{item_id}/cancel")
def cancel_requisition(
    item_id: int,
    payload: CancelPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    item = _item_or_404(db, item_id)
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细禁止取消报料")
    if item.requisition_status == "未报料":
        raise HTTPException(status_code=409, detail="该明细当前未报料")
    db.execute(
        update(RequisitionItem)
        .where(
            RequisitionItem.order_item_id == item.id,
            RequisitionItem.status == "有效",
        )
        .values(status="已取消")
    )
    item.inventory_deducted_qty = 0
    item.requisition_qty = None
    item.requisition_status = "未报料"
    item.special_process = "无"
    item.requisition_spec = None
    item.cardboard_len = None
    item.cardboard_width = None
    item.requisition_date = None
    item.supplier_delivery_time = None
    item.supplier_order_number = None
    item.requisition_remark = None
    _audit(
        db,
        user=user,
        action="CANCEL_REQUISITION",
        entity_id=item.id,
        details={"reason": payload.reason},
        description="取消采购报料",
    )
    db.commit()
    return _item_response(item)


@router.get("/search_history")
def search_history(
    keyword: str = Query(min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    query = (
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(OrderItem.requisition_status.in_(["已报料", "供应商已排单"]))
    )
    for token in keyword.split():
        pattern = f"%{token}%"
        query = query.where(
            or_(
                Customer.name.like(pattern),
                Customer.customer_code.like(pattern),
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                OrderItem.snapshot_product_name.like(pattern),
                OrderItem.snapshot_spec.like(pattern),
            )
        )
    rows = db.execute(
        query.order_by(
            OrderItem.requisition_date.desc(),
            OrderItem.id.desc(),
        ).limit(limit)
    ).all()
    return {
        "items": [
            {
                "item_id": item.id,
                "customer_name": customer.name,
                "order_number": display_order_number(order, registry),
                "display_order_number": display_order_number(order, registry),
                "product_id": product.id,
                "product_code": (
                    item.snapshot_product_code or product.product_code
                ),
                "product_name": item.snapshot_product_name,
                "specification": item.snapshot_spec,
                "material_id": product.material_id,
                "material": item.snapshot_material,
                "cardboard_len": item.cardboard_len,
                "cardboard_width": item.cardboard_width,
                "requisition_qty": item.requisition_qty,
                "special_process": item.special_process,
                "requisition_date": item.requisition_date,
            }
            for item, order, customer, product in rows
        ]
    }


@router.get("/merge-suggestions")
def merge_suggestions(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """返回待报料池中可合并的报料分组建议。
    合并口径：supplier_name + material_id + layer_count + flute_type +
              snapshot_report_length_mm + snapshot_report_width_mm +
              snapshot_crease_type + crease_left/middle/right 全部一致
    不修改订单，只返回展示数据。
    """
    registry = build_display_registry(db)
    rows = db.execute(
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            OrderItem.requisition_status == "未报料",
            OrderItem.material_status == "pending",
            Order.status != "cancelled",
        )
        .order_by(Order.delivery_date, OrderItem.id)
    ).all()

    def _merge_key(item: "OrderItem") -> tuple:
        return (
            item.snapshot_supplier_name or "",
            item.material_id or 0,
            item.layer_count or 0,
            (item.flute_type or "").upper(),
            item.snapshot_report_length_mm or 0,
            item.snapshot_report_width_mm or 0,
            item.snapshot_crease_type or "",
            item.snapshot_crease_left_mm or 0,
            item.snapshot_crease_middle_mm or 0,
            item.snapshot_crease_right_mm or 0,
        )

    from collections import defaultdict  # noqa: PLC0415
    groups: dict = defaultdict(list)
    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        key = _merge_key(item)
        # 只有报料尺寸有值才纳入合并建议
        if not (item.snapshot_report_length_mm and item.snapshot_report_width_mm):
            continue
        groups[key].append({
            "item_id": item.id,
            "order_number": display_order_number(order, registry),
            "customer_name": customer.name,
            "product_code": item.snapshot_product_code or product.product_code,
            "product_name": item.snapshot_product_name,
            "quantity": item.quantity,
            "delivery_date": order.delivery_date,
        })

    suggestions = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        supplier_name, material_id, layer_count, flute_type, \
            report_len, report_width, crease_type, \
            crease_left, crease_middle, crease_right = key
        crease_display = (
            f"{crease_left}×{crease_middle}×{crease_right}"
            if crease_type == "压线" and crease_middle
            else crease_type or "-"
        )
        suggestions.append({
            "key": str(key),
            "supplier_name": supplier_name,
            "material_id": material_id,
            "layer_count": layer_count,
            "flute_type": flute_type,
            "report_length_mm": report_len,
            "report_width_mm": report_width,
            "crease_type": crease_type,
            "crease_left_mm": crease_left,
            "crease_middle_mm": crease_middle,
            "crease_right_mm": crease_right,
            "crease_display": crease_display,
            "total_quantity": sum(m["quantity"] for m in members),
            "member_count": len(members),
            "members": members,
        })

    return {"suggestions": suggestions}


@router.get("/batches/{batch_id}/print")
def print_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    batch = db.get(Requisition, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="报料单不存在")
    rows = db.execute(
        select(
            RequisitionItem,
            OrderItem.snapshot_production_notes.label("production_notes"),
        )
        .outerjoin(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(
            RequisitionItem.requisition_id == batch.id,
            RequisitionItem.status == "有效",
        )
        .order_by(RequisitionItem.id)
    ).all()
    return {
        "id": batch.id,
        "requisition_number": batch.requisition_number,
        "requisition_date": batch.requisition_date,
        "supplier_name": batch.supplier_name,
        "items": [
            {
                "product_code": row.product_code_snapshot,
                "product_name": row.product_name_snapshot,
                "material": row.material_snapshot,
                "specification": (
                    f"{_plain(row.cardboard_len)}×{_plain(row.cardboard_width)}"
                ),
                "quantity": row.requisition_qty,
                "special_process": row.special_process,
                "production_notes": production_notes,
            }
            for row, production_notes in rows
        ],
        "total_quantity": sum(row.requisition_qty for row, _ in rows),
    }


# ── 供应商报料单 ──────────────────────────────────────────────────────────────

class SupplierOrderMemberPayload(BaseModel):
    item_id: int | None = None
    stock_deduction_qty: int = Field(default=0, ge=0)
    requisition_qty: int | None = Field(default=None, ge=0)
    order_number: str | None = None
    product_code: str | None = None
    product_name: str | None = None
    quantity: int = 0
    customer_name: str | None = None
    delivery_date: str | None = None


class SupplierOrderCreatePayload(BaseModel):
    supplier_name: str | None = None
    material_id: int | None = None
    layer_count: int | None = None
    flute_type: str | None = None
    report_length_mm: int | None = None
    report_width_mm: int | None = None
    crease_type: str | None = None
    crease_left_mm: int | None = None
    crease_middle_mm: int | None = None
    crease_right_mm: int | None = None
    remark: str | None = None
    members: list[SupplierOrderMemberPayload] = Field(default_factory=list)


def _supplier_order_number(db: Session) -> str:
    """生成供应商报料单号，格式：SRO-YYYYMMDD-NNNN"""
    from datetime import date as _date
    today_str = _date.today().strftime("%Y%m%d")
    prefix = f"SRO-{today_str}-"
    count = db.scalar(
        select(func.count(SupplierRequisitionOrder.id)).where(
            SupplierRequisitionOrder.order_number.like(f"{prefix}%")
        )
    ) or 0
    return f"{prefix}{count + 1:04d}"


def _supplier_order_dict(order: SupplierRequisitionOrder) -> dict:
    crease_display = (
        f"{order.crease_left_mm}+{order.crease_middle_mm}+{order.crease_right_mm}"
        if order.crease_type == "压线" and order.crease_middle_mm
        else order.crease_type or "-"
    )
    return {
        "id": order.id,
        "order_number": order.order_number,
        "supplier_name": order.supplier_name,
        "material_id": order.material_id,
        "layer_count": order.layer_count,
        "flute_type": order.flute_type,
        "report_length_mm": order.report_length_mm,
        "report_width_mm": order.report_width_mm,
        "crease_type": order.crease_type,
        "crease_left_mm": order.crease_left_mm,
        "crease_middle_mm": order.crease_middle_mm,
        "crease_right_mm": order.crease_right_mm,
        "crease_display": crease_display,
        "total_quantity": order.total_quantity,
        "stock_deduction_qty": order.stock_deduction_qty,
        "requisition_qty": order.requisition_qty,
        "remark": order.remark,
        "status": order.status,
        "created_at": order.created_at,
        "voided_at": order.voided_at,
        "items": [
            {
                "id": item.id,
                "order_item_id": item.order_item_id,
                "order_number": item.order_number,
                "product_code": item.product_code,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "stock_deduction_qty": item.stock_deduction_qty,
                "requisition_qty": item.requisition_qty,
                "customer_name": item.customer_name,
                "delivery_date": item.delivery_date,
            }
            for item in order.items
        ],
    }


@router.post("/supplier-orders", status_code=status.HTTP_201_CREATED)
def create_supplier_order(
    payload: SupplierOrderCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """从合并建议生成供应商报料单，同步更新 order_items 的 requisition_status。"""
    if not payload.members:
        raise HTTPException(status_code=400, detail="至少需要一条明细")

    order_number = _supplier_order_number(db)
    total_qty = sum(m.quantity for m in payload.members)
    total_deduct = sum(m.stock_deduction_qty for m in payload.members)
    total_req = sum(
        m.requisition_qty if m.requisition_qty is not None else max(m.quantity - m.stock_deduction_qty, 0)
        for m in payload.members
    )

    order = SupplierRequisitionOrder(
        order_number=order_number,
        supplier_name=payload.supplier_name,
        material_id=payload.material_id,
        layer_count=payload.layer_count,
        flute_type=payload.flute_type,
        report_length_mm=payload.report_length_mm,
        report_width_mm=payload.report_width_mm,
        crease_type=payload.crease_type,
        crease_left_mm=payload.crease_left_mm,
        crease_middle_mm=payload.crease_middle_mm,
        crease_right_mm=payload.crease_right_mm,
        total_quantity=total_qty,
        stock_deduction_qty=total_deduct,
        requisition_qty=total_req,
        remark=payload.remark,
        status="confirmed",
        created_by=user.id,
    )
    db.add(order)
    db.flush()

    for m in payload.members:
        req_qty = m.requisition_qty if m.requisition_qty is not None else max(m.quantity - m.stock_deduction_qty, 0)
        db.add(SupplierRequisitionOrderItem(
            supplier_order_id=order.id,
            order_item_id=m.item_id,
            order_number=m.order_number,
            product_code=m.product_code,
            product_name=m.product_name,
            quantity=m.quantity,
            stock_deduction_qty=m.stock_deduction_qty,
            requisition_qty=req_qty,
            customer_name=m.customer_name,
            delivery_date=date.fromisoformat(m.delivery_date) if m.delivery_date else None,
        ))
        # 更新 order_item 状态
        oi = db.get(OrderItem, m.item_id) if m.item_id else None
        if oi:
            oi.requisition_status = "已报料"
            oi.inventory_deducted_qty = m.stock_deduction_qty
            oi.requisition_qty = req_qty

    db.commit()
    db.refresh(order)
    return _supplier_order_dict(order)


@router.get("/supplier-orders")
def list_supplier_orders(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """列出供应商报料单（最新在前）。"""
    q = select(SupplierRequisitionOrder)
    if status_filter:
        q = q.where(SupplierRequisitionOrder.status == status_filter)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    orders = db.scalars(
        q.order_by(SupplierRequisitionOrder.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_supplier_order_dict(o) for o in orders],
    }


@router.get("/supplier-orders/{order_id}")
def get_supplier_order(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    return _supplier_order_dict(order)


@router.put("/supplier-orders/{order_id}/void")
def void_supplier_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """作废供应商报料单，将关联的 order_items 恢复为待报料。"""
    from datetime import datetime as _datetime
    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    if order.status == "voided":
        raise HTTPException(status_code=400, detail="该报料单已作废")

    order.status = "voided"
    order.voided_at = _datetime.now()

    for item in order.items:
        if item.order_item_id:
            oi = db.get(OrderItem, item.order_item_id)
            if oi and oi.requisition_status == "已报料":
                oi.requisition_status = "未报料"
                oi.inventory_deducted_qty = 0
                oi.requisition_qty = None

    db.commit()
    db.refresh(order)
    return _supplier_order_dict(order)
