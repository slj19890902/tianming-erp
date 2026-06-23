from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import (
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import Requisition, RequisitionItem
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
    calculate_draft_cost,
    extract_text_from_pdf_bytes,
    file_sha256,
    match_import_draft,
    parse_purchase_order_text,
)
from app.services.product_import import (
    NewProductError,
    NewProductInput,
    parse_dimensions,
    resolve_or_create_product,
)


router = APIRouter()
can_create = RoleChecker(["admin", "sales"])
can_read = RoleChecker(["admin", "finance", "sales", "workshop"])
admin_only = RoleChecker(["admin"])
MONEY_QUANTUM = Decimal("0.00")
ORDER_STATUSES = {
    "pending_confirmation",
    "pending_production",
    "production",
    "pending_delivery",
    "partially_delivered",
    "pending_reconciliation",
    "pending_invoice",
    "pending_payment",
    "completed",
    "archived",
    "closed",
    "dead",
    "cancelled",
    "delivered",
}
FINAL_ORDER_STATUSES = {"completed", "archived", "closed", "dead", "cancelled", "delivered"}


_PRODUCT_ID_SENTINELS = {"", "new_product", "null", "undefined", "none", "nan"}


class OrderItemCreate(BaseModel):
    product_id: int | None = None
    quantity: int
    unit_price: Decimal
    product_code: str | None = None
    product_name: str | None = None
    material: str | None = None
    specification: str | None = None
    is_new_product: bool = False
    material_id: int | None = None

    @field_validator("product_id", "material_id", mode="before")
    @classmethod
    def _coerce_id_sentinels(cls, value: object) -> object:
        # Defense-in-depth: tolerate stale frontend payloads that send empty
        # strings or sentinel tokens instead of null for an unselected product.
        if isinstance(value, str):
            if value.strip().casefold() in _PRODUCT_ID_SENTINELS:
                return None
            return value.strip()
        return value


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


class DraftRematchRequest(BaseModel):
    draft: dict
    customer_id: int


class CostPreviewRequest(BaseModel):
    product_id: int
    material_id: int | None = None


class OrderStatusRequest(BaseModel):
    status: str
    remark: str


class WorkflowRollbackRequest(BaseModel):
    reason: str


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
    db: Session | None = None,
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
        "items": [],
    }
    for item in order.items:
        cost_reference = (
            calculate_draft_cost(db, item.product_id)
            if db is not None and user.role != "workshop"
            else {}
        )
        item_data = {
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
                **cost_reference,
        }
        if item_data.get("estimated_cost") is not None:
            item_data["estimated_gross_profit"] = str(
                (Decimal(str(item.unit_price)) - Decimal(item_data["estimated_cost"])).quantize(
                    Decimal("0.0001")
                )
            )
        data["items"].append(item_data)
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

    history_condition = Order.order_number.like("RUIDA-%")
    if status_filter == "business":
        ids_query = ids_query.where(~history_condition)
    elif status_filter == "history":
        ids_query = ids_query.where(history_condition)
    elif status_filter == "unfinished":
        ids_query = ids_query.where(
            ~history_condition,
            Order.status.in_(
                (
                    "pending_confirmation",
                    "pending_production",
                    "production",
                    "pending_delivery",
                    "partially_delivered",
                )
            ),
            Order.items.any(
                (OrderItem.delivered_quantity < OrderItem.quantity)
                & OrderItem.is_force_closed.is_(False)
                & (OrderItem.requisition_status != "已结算")
            ),
        )
    elif status_filter:
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
    unfinished_total = db.scalar(
        select(func.count(Order.id)).where(
            ~history_condition,
            Order.status.in_(
                (
                    "pending_confirmation",
                    "pending_production",
                    "production",
                    "pending_delivery",
                    "partially_delivered",
                )
            ),
            Order.items.any(
                (OrderItem.delivered_quantity < OrderItem.quantity)
                & OrderItem.is_force_closed.is_(False)
                & (OrderItem.requisition_status != "已结算")
            ),
        )
    ) or 0
    return {
        "total": total,
        "unfinished_total": unfinished_total,
        "page": page,
        "page_size": page_size,
        "items": [
            _order_response(
                order,
                user,
                db=db,
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
        draft["file_hash"] = file_sha256(content)
        return match_import_draft(db, draft)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail="文件识别失败，请检查文件内容后重试") from error


@router.post("/pdf-preview-batch")
async def preview_order_pdf_batch(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    _user: User = Depends(can_create),
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个 PDF 文件")
    drafts: list[dict] = []
    seen_hashes: set[str] = set()
    for file in files:
        filename = (file.filename or "uploaded.pdf").strip()
        try:
            if not filename.lower().endswith(".pdf"):
                raise ValueError("只支持 PDF 文件")
            content = await file.read()
            if not content:
                raise ValueError("文件为空")
            digest = file_sha256(content)
            if digest in seen_hashes:
                drafts.append(
                    {
                        "source_name": filename,
                        "file_hash": digest,
                        "recognition_status": "duplicate_skipped",
                        "duplicate_status": "duplicate_skipped",
                        "duplicate_reason": "本批次已上传相同文件",
                        "items": [],
                        "warnings": ["本批次已上传相同文件，已跳过。"],
                    }
                )
                continue
            seen_hashes.add(digest)
            text = extract_text_from_pdf_bytes(content)
            draft = parse_purchase_order_text(text, source_name=filename)
            draft["file_hash"] = digest
            drafts.append(match_import_draft(db, draft))
        except ValueError as error:
            drafts.append(
                {
                    "source_name": filename,
                    "recognition_status": "failed",
                    "duplicate_status": None,
                    "items": [],
                    "warnings": [str(error)],
                }
            )
        except Exception:
            drafts.append(
                {
                    "source_name": filename,
                    "recognition_status": "failed",
                    "duplicate_status": None,
                    "items": [],
                    "warnings": ["文件识别失败，请检查文件内容后重试。"],
                }
            )
    return {"batch_count": len(files), "drafts": drafts}


@router.post("/draft-rematch")
def rematch_order_draft(
    payload: DraftRematchRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(can_create),
) -> dict:
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    return match_import_draft(db, payload.draft, customer_id=payload.customer_id)


@router.post("/cost-preview")
def preview_order_cost(
    payload: CostPreviewRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(can_create),
) -> dict:
    try:
        return calculate_draft_cost(db, payload.product_id, payload.material_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _order_has_flow_records(db: Session, order_id: int) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(DeliveryItem)
            .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .where(OrderItem.order_id == order_id)
        )
    )


@router.put("/{order_id}/status")
def update_order_status(
    order_id: int,
    payload: OrderStatusRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    target = payload.status.strip()
    remark = payload.remark.strip()
    if target not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="订单状态无效")
    if target in {"dead", "closed", "archived", "cancelled"} and not remark:
        raise HTTPException(status_code=400, detail="标记死单、已结档、已归档或已作废时必须填写备注")
    before = order.status
    order.status = target
    if remark:
        order.remark = remark
    if target in FINAL_ORDER_STATUSES:
        for item in order.items:
            item.is_force_closed = True
    db.add(
        OperationLog(
            user_id=user.id,
            action="STATUS",
            resource="Order",
            details=json.dumps(
                {"before": before, "after": target, "remark": remark},
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="order",
            entity_id=order.id,
            description=f"订单状态变更为{target}",
        )
    )
    db.commit()
    customer = db.get(Customer, order.customer_id)
    return _order_response(
        order,
        user,
        db=db,
        customer_name=customer.name if customer else None,
    )


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(
    order_id: int,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> Response:
    if not confirm:
        raise HTTPException(status_code=400, detail="删除订单需要二次确认")
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if _order_has_flow_records(db, order.id):
        raise HTTPException(
            status_code=409,
            detail="该订单已有送货、开票或收款关联，不能直接删除；请标记死单或已结档。",
        )
    details = {
        "order_number": order.order_number,
        "customer_id": order.customer_id,
        "customer_po": order.customer_po,
        "item_count": len(order.items),
    }
    db.add(
        OperationLog(
            user_id=user.id,
            action="DELETE",
            resource="Order",
            details=json.dumps(details, ensure_ascii=False),
            username=user.username,
            role=user.role,
            entity_type="order",
            entity_id=order.id,
            description="删除无业务关联订单",
        )
    )
    db.delete(order)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{order_id}/rollback-workflow")
def rollback_order_workflow(
    order_id: int,
    payload: WorkflowRollbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="撤回原因不能为空")
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.order_number.startswith("RUIDA-"):
        raise HTTPException(status_code=409, detail="历史订单禁止执行流程撤回")
    item_ids = [item.id for item in order.items]
    delivery_items = db.scalars(
        select(DeliveryItem).where(DeliveryItem.order_item_id.in_(item_ids))
    ).all()
    delivery_ids = {item.delivery_id for item in delivery_items}
    for delivery_id in delivery_ids:
        foreign_line = db.scalar(
            select(DeliveryItem.id)
            .where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.order_item_id.not_in(item_ids),
            )
            .limit(1)
        )
        if foreign_line is not None:
            raise HTTPException(
                status_code=409,
                detail="该订单与其他订单共用送货单，不能自动撤回，请先拆分处理。",
            )
    receipts = (
        db.scalars(select(ReturnReceipt).where(ReturnReceipt.delivery_id.in_(delivery_ids))).all()
        if delivery_ids
        else []
    )
    receipt_ids = [row.id for row in receipts]
    receipt_item_ids = (
        list(
            db.scalars(
                select(ReturnReceiptItem.id).where(
                    ReturnReceiptItem.return_receipt_id.in_(receipt_ids)
                )
            ).all()
        )
        if receipt_ids
        else []
    )
    statement_ids = (
        set(
            db.scalars(
                select(StatementItem.statement_id).where(
                    StatementItem.return_receipt_item_id.in_(receipt_item_ids)
                )
            ).all()
        )
        if receipt_item_ids
        else set()
    )
    for statement_id in statement_ids:
        foreign_item = db.scalar(
            select(StatementItem.id)
            .where(
                StatementItem.statement_id == statement_id,
                StatementItem.return_receipt_item_id.not_in(receipt_item_ids),
            )
            .limit(1)
        )
        if foreign_item is not None:
            raise HTTPException(
                status_code=409,
                detail="该订单与其他订单共用对账单，不能自动撤回，请先拆分处理。",
            )
    try:
        if statement_ids:
            db.execute(delete(SettlementRecord).where(SettlementRecord.statement_id.in_(statement_ids)))
            db.execute(delete(Invoice).where(Invoice.statement_id.in_(statement_ids)))
            db.execute(delete(StatementItem).where(StatementItem.statement_id.in_(statement_ids)))
            db.execute(delete(Statement).where(Statement.id.in_(statement_ids)))
        if receipt_ids:
            db.execute(delete(ReturnReceiptItem).where(ReturnReceiptItem.return_receipt_id.in_(receipt_ids)))
            db.execute(delete(ReturnReceipt).where(ReturnReceipt.id.in_(receipt_ids)))
        if delivery_ids:
            db.execute(delete(DeliveryItem).where(DeliveryItem.delivery_id.in_(delivery_ids)))
            db.execute(delete(Delivery).where(Delivery.id.in_(delivery_ids)))
        requisition_ids = set(
            db.scalars(
                select(RequisitionItem.requisition_id).where(
                    RequisitionItem.order_item_id.in_(item_ids)
                )
            ).all()
        )
        db.execute(delete(RequisitionItem).where(RequisitionItem.order_item_id.in_(item_ids)))
        for requisition_id in requisition_ids:
            if db.scalar(
                select(RequisitionItem.id)
                .where(RequisitionItem.requisition_id == requisition_id)
                .limit(1)
            ) is None:
                db.execute(delete(Requisition).where(Requisition.id == requisition_id))
        for item in order.items:
            item.delivered_quantity = 0
            item.is_force_closed = False
            item.material_status = "pending"
            item.material_received_at = None
            item.material_received_by = None
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
        order.status = "pending_production"
        order.payment_status = "unpaid"
        db.add(
            OperationLog(
                user_id=user.id,
                action="ROLLBACK_WORKFLOW",
                resource="Order",
                details=json.dumps(
                    {
                        "reason": reason,
                        "delivery_ids": sorted(delivery_ids),
                        "receipt_ids": receipt_ids,
                        "statement_ids": sorted(statement_ids),
                    },
                    ensure_ascii=False,
                ),
                username=user.username,
                role=user.role,
                entity_type="order",
                entity_id=order.id,
                description="订单撤回到未送货未报料状态",
            )
        )
        db.commit()
        customer = db.get(Customer, order.customer_id)
        return _order_response(order, user, db=db, customer_name=customer.name if customer else None)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


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
        db=db,
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
        db=db,
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

        customer_po = (payload.customer_po or "").strip() or None

        new_product_cache: dict[str, Product] = {}
        resolved_products: dict[int, Product] = {}
        for index, item_payload in enumerate(payload.items, start=1):
            if item_payload.product_id is not None and not item_payload.is_new_product:
                product = db.scalar(
                    select(Product)
                    .options(joinedload(Product.material))
                    .where(Product.id == item_payload.product_id)
                )
                if product is None:
                    raise HTTPException(
                        status_code=400, detail=f"第{index}条明细产品不存在"
                    )
                if product.deleted_at is not None:
                    raise HTTPException(
                        status_code=400, detail=f"第{index}条明细产品已删除"
                    )
                if not product.is_active:
                    raise HTTPException(
                        status_code=400, detail="该纸箱已停用，不能用于新建订单"
                    )
                if product.customer_id != customer.id:
                    raise HTTPException(
                        status_code=400, detail=f"第{index}条明细产品不属于当前客户"
                    )
            else:
                spec = (item_payload.specification or "").strip() or None
                length_mm, width_mm, height_mm = parse_dimensions(spec)
                try:
                    new_sale_price = Decimal(str(item_payload.unit_price))
                except (InvalidOperation, ValueError):
                    new_sale_price = None
                try:
                    product = resolve_or_create_product(
                        db,
                        customer=customer,
                        data=NewProductInput(
                            inventory_code=(item_payload.product_code or "").strip(),
                            product_name=(item_payload.product_name or "").strip(),
                            specification=spec,
                            material_id=item_payload.material_id,
                            length_mm=length_mm,
                            width_mm=width_mm,
                            height_mm=height_mm,
                            sale_unit_price=(
                                new_sale_price
                                if new_sale_price is not None and new_sale_price > 0
                                else None
                            ),
                        ),
                        cache=new_product_cache,
                    )
                except NewProductError as error:
                    raise HTTPException(
                        status_code=400, detail=f"第{index}条明细{error}"
                    ) from error
            resolved_products[index] = product

        if customer_po:
            existing_orders = db.scalars(
                select(Order)
                .options(selectinload(Order.items))
                .where(
                    Order.customer_id == customer.id,
                    Order.customer_po == customer_po,
                )
            ).all()
            incoming_signature = sorted(
                (
                    resolved_products[index].id,
                    item.quantity,
                    str(Decimal(str(item.unit_price)).quantize(Decimal("0.0001"))),
                    (
                        (item.specification or "").strip()
                        or _snapshot_spec(resolved_products[index])
                        or ""
                    ),
                )
                for index, item in enumerate(payload.items, start=1)
            )
            for existing_order in existing_orders:
                existing_signature = sorted(
                    (
                        item.product_id,
                        item.quantity,
                        str(Decimal(str(item.unit_price)).quantize(Decimal("0.0001"))),
                        (item.snapshot_spec or "").strip(),
                    )
                    for item in existing_order.items
                )
                if existing_signature == incoming_signature:
                    raise HTTPException(
                        status_code=409,
                        detail="系统中已存在相同客户、客户单号和明细的订单，未重复生成。",
                    )

        order_date = payload.order_date or date.today()
        order = Order(
            order_number=reserve_next_order_number(db, order_date),
            customer_id=customer.id,
            customer_po=customer_po,
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

            product = resolved_products[index]

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
            db=db,
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
