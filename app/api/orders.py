from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
from app.models.tianhua_pre_delivery import (
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
    TianhuaPreDeliveryImportItem,
)
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
from app.services import material_pricing
from app.services.order_pdf_import import (
    PARSE_STATUS_LABELS,
    PdfParseError,
    calculate_draft_cost,
    extract_text_from_pdf_bytes,
    file_sha256,
    match_import_draft,
    parse_purchase_order_text,
)
from app.services.pdf_customer_templates import load_active_pdf_template_rules
from app.services.pdf_ocr import ocr_pdf_bytes, should_use_ocr
from app.services.product_import import (
    NewProductError,
    NewProductInput,
    parse_dimensions,
    resolve_or_create_product,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    active_finished_reservations_by_item_ids,
    release_active_finished_reservations_for_items,
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


def _box_style_uses_tongue(box_style: str | None) -> bool:
    value = (box_style or "").strip().upper()
    if not value:
        return True
    if any(marker in value for marker in ("A3", "天地盖", "平卡", "刀卡", "隔板", "异形", "其他")):
        return False
    return any(marker in value for marker in ("A1", "0201", "围套", "半开槽", "全搭盖", "0200", "0203"))


def _box_style_uses_splice(box_style: str | None) -> bool:
    value = (box_style or "").strip().upper()
    if not value:
        return True
    return "A1" in value or "0201" in value


# Terminal / archived statuses that should NOT appear in the day-to-day
# "business" (日常订单) view. Active orders — including freshly saved PDF
# imports that are still pending_production / awaiting requisition — stay in
# business. "completed" is intentionally kept (see
# test_business_orders_show_completed_but_badge_counts_only_undelivered): a
# finished-but-not-yet-archived order is still part of daily work, while the
# unfinished badge only counts undelivered rows.
_BUSINESS_EXCLUDED_STATUSES = ("dead", "cancelled", "closed", "archived")


class OrderItemCreate(BaseModel):
    product_id: int | None = None
    quantity: int | float
    unit_price: Decimal
    product_code: str | None = None
    product_name: str | None = None
    material: str | None = None
    specification: str | None = None
    customer_model: str | None = None  # v0.19.1: TH型号 / 客户型号
    production_notes: str | None = None  # v0.19.2-A: 生产/印刷/打勾/摆放/日文警示等行级说明
    is_new_product: bool = False
    material_id: int | None = None
    layer_count: int | None = None   # v0.19.2-B: 常用箱层数（自动带出）
    flute_type: str | None = None    # v0.19.2-B: 常用箱实际楞型（自动带出）
    temp_drawing_file: str | None = None   # 新建订单前临时上传的图纸路径
    drawing_save_option: str | None = None  # "order_only"|"save_to_product"|"overwrite_product"

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
    production_notes: str | None = None  # v0.19.2-A
    # v0.19.2-B: 材质联动字段
    material_id: int | None = None
    layer_count: int | None = None
    flute_type: str | None = None
    # v0.19.2-B: 报料快照（从常用箱编辑/PDF 草稿编辑时写入）
    snapshot_report_length_mm: int | None = None
    snapshot_report_width_mm: int | None = None
    snapshot_crease_type: str | None = None
    snapshot_crease_left_mm: int | None = None
    snapshot_crease_middle_mm: int | None = None
    snapshot_crease_right_mm: int | None = None
    snapshot_report_notes: str | None = None
    snapshot_base_report_length_mm: int | None = None
    snapshot_base_report_width_mm: int | None = None
    snapshot_base_crease_type: str | None = None
    snapshot_base_crease_left_mm: int | None = None
    snapshot_base_crease_middle_mm: int | None = None
    snapshot_base_crease_right_mm: int | None = None
    snapshot_base_report_notes: str | None = None
    snapshot_splice_mode: str | None = None
    snapshot_pieces_per_box: int | None = None
    snapshot_flap_mm: int | None = None
    # v0.20.9: 订单编辑页中的常用箱生产字段；有关联产品时同事务同步。
    sync_product: bool = True
    box_style: str | None = None
    length_mm: int | None = Field(default=None, gt=0)
    width_mm: int | None = Field(default=None, gt=0)
    height_mm: int | None = Field(default=None, gt=0)
    production_process: str | None = None
    print_content: str | None = None
    product_remark: str | None = None


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
    import_integrity_status: str | None = None
    import_integrity_errors: list[str] | None = None

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


def _validated_order_quantity(value: int | float, index: int) -> int:
    decimal_value = Decimal(str(value))
    if decimal_value <= 0:
        raise HTTPException(status_code=400, detail=f"第{index}条明细数量必须大于0")
    if decimal_value != decimal_value.to_integral_value():
        raise HTTPException(
            status_code=400,
            detail="当前 PDF 识别存在非整数数量，请人工确认并修改后再保存。",
        )
    return int(decimal_value)


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
    flute_type: str | None = None   # v0.19.2-B: 传楞型以计入加价


class OrderStatusRequest(BaseModel):
    status: str
    remark: str


class WorkflowRollbackRequest(BaseModel):
    reason: str


class OrderGroupDeleteRequest(BaseModel):
    order_ids: list[int] = Field(min_length=1, max_length=200)
    confirm: bool = False


def _plain_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _order_item_cost_totals(
    quantity: int,
    unit_price: Decimal,
    subtotal: Decimal,
    unit_cost: Decimal,
) -> dict[str, str]:
    unit_gross_profit = unit_price - unit_cost
    total_cost = unit_cost * Decimal(quantity)
    total_gross_profit = subtotal - total_cost
    return {
        "estimated_gross_profit": str(
            unit_gross_profit.quantize(Decimal("0.0001"))
        ),
        "unit_estimated_cost": str(unit_cost.quantize(Decimal("0.0001"))),
        "unit_estimated_gross_profit": str(
            unit_gross_profit.quantize(Decimal("0.0001"))
        ),
        "sale_amount": str(subtotal.quantize(MONEY_QUANTUM)),
        "total_estimated_cost": str(total_cost.quantize(MONEY_QUANTUM)),
        "total_estimated_gross_profit": str(
            total_gross_profit.quantize(MONEY_QUANTUM)
        ),
    }


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
    reservation_map = (
        active_finished_reservations_by_item_ids(
            db, [item.id for item in order.items]
        )
        if db is not None
        else {}
    )
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
        finished_reserved_quantity = reservation_map.get(item.id, 0)
        production_required_quantity = max(
            item.quantity - finished_reserved_quantity, 0
        )
        cost_reference = (
            calculate_draft_cost(db, item.product_id, item.material_id)
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
                "snapshot_customer_model": item.snapshot_customer_model,  # v0.19.1
                "snapshot_production_notes": item.snapshot_production_notes,  # v0.19.2-A
                "display_material": _display_material(item.snapshot_material),
                # v0.19.2-B: 常用箱层数/楞型/供应商/克重/图纸
                "layer_count": item.layer_count,
                "flute_type": item.flute_type,
                "material_id": item.material_id,
                "snapshot_supplier_name": item.snapshot_supplier_name,
                "snapshot_weight": item.snapshot_weight,
                "drawing_file": item.drawing_file,
                # v0.19.2-B: 报料快照
                "snapshot_report_length_mm": item.snapshot_report_length_mm,
                "snapshot_report_width_mm": item.snapshot_report_width_mm,
                "snapshot_crease_type": item.snapshot_crease_type,
                "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
                "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
                "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
                "snapshot_report_notes": item.snapshot_report_notes,
                "snapshot_base_report_length_mm": item.snapshot_base_report_length_mm,
                "snapshot_base_report_width_mm": item.snapshot_base_report_width_mm,
                "snapshot_base_crease_type": item.snapshot_base_crease_type,
                "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
                "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
                "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
                "snapshot_base_report_notes": item.snapshot_base_report_notes,
                "snapshot_splice_mode": item.snapshot_splice_mode,
                "snapshot_pieces_per_box": item.snapshot_pieces_per_box,
                "snapshot_flap_mm": item.snapshot_flap_mm,
                # v0.19.2-B: 常用箱图纸（展开明细/详情图纸 fallback 用）
                "product_drawing_file": (
                    item.product.drawings[0].image_path
                    if item.product_id
                    and item.product is not None
                    and item.product.drawings
                    else None
                ),
                "inventory_deducted_qty": item.inventory_deducted_qty,
                "finished_inventory_reserved_qty": finished_reserved_quantity,
                "production_required_qty": production_required_quantity,
                "fully_covered_by_finished_inventory": (
                    production_required_quantity == 0
                ),
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
            unit_cost = Decimal(item_data["estimated_cost"])
            item_data.update(
                _order_item_cost_totals(
                    item.quantity,
                    Decimal(str(item.unit_price)),
                    Decimal(str(item.subtotal)),
                    unit_cost,
                )
            )
        else:
            item_data.update(
                {
                    "unit_estimated_cost": None,
                    "unit_estimated_gross_profit": None,
                    "sale_amount": str(Decimal(str(item.subtotal)).quantize(MONEY_QUANTUM)),
                    "total_estimated_cost": None,
                    "total_estimated_gross_profit": None,
                }
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
        ids_query = ids_query.where(
            ~history_condition,
            Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
        )
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

    # History (RUIDA legacy) keeps chronological order_date ordering. All other
    # views — especially "business" — sort by creation time so a freshly saved
    # order surfaces at the top even when its order_date is back-dated to the
    # source document date (e.g. PDF imports), instead of being buried below
    # newer-dated rows where users assume it "disappeared".
    if status_filter == "history":
        ids_query = ids_query.order_by(Order.order_date.desc(), Order.id.desc())
    else:
        ids_query = ids_query.order_by(
            func.coalesce(Order.updated_at, Order.created_at).desc(),
            Order.id.desc(),
        )
    total = db.scalar(select(func.count()).select_from(ids_query.subquery())) or 0
    page_ids = list(
        db.scalars(ids_query.offset((page - 1) * page_size).limit(page_size)).all()
    )

    orders: list[Order] = []
    if page_ids:
        loaded = db.scalars(
            select(Order)
            .options(
                selectinload(Order.items).selectinload(OrderItem.product).selectinload(
                    Product.drawings  # type: ignore[attr-defined]
                )
            )
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


def _pdf_failure_draft(
    filename: str,
    error: PdfParseError,
    digest: str | None = None,
) -> dict:
    """v0.19.2-A A-5: 把结构化解析失败转成前端可显示的草稿（含具体失败原因）。"""
    label = PARSE_STATUS_LABELS.get(error.parse_status, "解析失败")
    return {
        "source_name": filename,
        "file_hash": digest,
        "recognition_status": "failed",
        "parse_status": error.parse_status,
        "parse_status_label": label,
        "message": error.message,
        "duplicate_status": None,
        "items": [],
        "warnings": [f"{label}：{error.message}"],
    }


def _parse_order_pdf_preview(
    content: bytes,
    filename: str,
    template_rules: list[dict],
) -> dict:
    """Parse an order PDF for preview, using OCR only when text parsing needs it."""
    text = extract_text_from_pdf_bytes(content)
    draft: dict | None = None
    parse_error: PdfParseError | None = None

    if text and text.strip():
        try:
            draft = parse_purchase_order_text(
                text,
                source_name=filename,
                template_rules=template_rules,
            )
        except PdfParseError as error:
            parse_error = error

    if should_use_ocr(text, draft):
        ocr_text, ocr_method = ocr_pdf_bytes(content)
        if ocr_text and ocr_method not in {"ocr_unavailable", "ocr_failed"}:
            try:
                return parse_purchase_order_text(
                    ocr_text,
                    source_name=filename,
                    template_rules=template_rules,
                )
            except PdfParseError as error:
                parse_error = error

    if draft is not None:
        return draft
    if parse_error is not None:
        raise parse_error
    return parse_purchase_order_text(
        text or "",
        source_name=filename,
        template_rules=template_rules,
    )


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
    template_rules = load_active_pdf_template_rules(db)
    try:
        draft = _parse_order_pdf_preview(content, filename, template_rules)
        draft["file_hash"] = file_sha256(content)
        return match_import_draft(db, draft)
    except PdfParseError as error:
        return _pdf_failure_draft(filename, error, digest=file_sha256(content))
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
    template_rules = load_active_pdf_template_rules(db)
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
            draft = _parse_order_pdf_preview(content, filename, template_rules)
            draft["file_hash"] = digest
            drafts.append(match_import_draft(db, draft))
        except PdfParseError as error:
            drafts.append(_pdf_failure_draft(filename, error, digest=digest))
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
    """纸板成本预估；若传 flute_type 则计入楞型加价（v0.19.2-B）。"""
    try:
        result = calculate_draft_cost(db, payload.product_id, payload.material_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    # v0.19.2-B：若有楞型，用 get_effective_material_price 重新算成本
    if payload.flute_type and result.get("cost_status") == "calculated":
        from app.services.order_pdf_import import _cost_reference as _ocr
        from sqlalchemy.orm import joinedload as _jl
        product = db.scalar(
            select(Product).options(_jl(Product.material)).where(Product.id == payload.product_id)
        )
        if product is not None:
            mat = db.get(Material, payload.material_id) if payload.material_id else product.material
            if mat and mat.quote_price is not None:
                eff = material_pricing.get_effective_material_price(
                    db,
                    base_price=mat.quote_price,
                    supplier_name=mat.supplier_name,
                    layer_count=product.layer_count or (mat.layer_count if mat else None),
                    flute_type=payload.flute_type,
                )
                if eff["effective_price"] is not None:
                    from app.services.pricing import calculate_price, PricingError
                    try:
                        pr = calculate_price(
                            box_category=product.box_category,
                            board_square_price=eff["effective_price"],
                            length_mm=product.length_mm,
                            width_mm=product.width_mm,
                            height_mm=product.height_mm,
                            unfolded_length_mm=product.default_cardboard_length,
                            unfolded_width_mm=product.default_cardboard_width,
                        )
                        result["estimated_cost"] = str(pr.unit_price)
                        result["flute_delta"] = eff["flute_delta"]
                        result["base_material_price"] = eff["base_price"]
                    except PricingError:
                        pass
    return result


@router.post("/draft-drawing", status_code=status.HTTP_200_OK)
async def upload_draft_drawing(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    """新建订单未保存前的临时图纸上传。写入临时目录，不写 DB，前端持有路径直到提交。"""
    import os, uuid
    content = await file.read()
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() or "png"
    fname = f"draft_{uuid.uuid4().hex}.{ext}"
    draft_dir = "static/uploads/order_drafts"
    os.makedirs(draft_dir, exist_ok=True)
    fpath = f"{draft_dir}/{fname}"
    with open(fpath, "wb") as fp:
        fp.write(content)
    return {"temp_path": f"/static/uploads/order_drafts/{fname}", "filename": file.filename or fname}


@router.post("/items/{item_id}/drawing", status_code=status.HTTP_200_OK)
async def upload_order_item_drawing(
    item_id: int,
    file: UploadFile = File(...),
    save_to_product: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    """上传订单明细图纸。默认只写 order_item；save_to_product=true 时同步写入 product_drawings（需用户确认）。"""
    import os, uuid
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    content = await file.read()
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() or "png"
    fname = f"{uuid.uuid4().hex}.{ext}"
    draw_dir = "static/uploads/drawings"
    os.makedirs(draw_dir, exist_ok=True)
    fpath = f"{draw_dir}/{fname}"
    with open(fpath, "wb") as fp:
        fp.write(content)
    item.drawing_file = f"/static/uploads/drawings/{fname}"
    if save_to_product and item.product_id:
        from app.models.product_drawing import ProductDrawing
        product_drawing = ProductDrawing(
            product_id=item.product_id,
            image_path=item.drawing_file,
            thumbnail_path=item.drawing_file,
            uploaded_by=user.id,
        )
        db.add(product_drawing)
    db.commit()
    return {"drawing_file": item.drawing_file, "saved_to_product": save_to_product}


PREDELIVERY_INVALIDATION_ACTIONS = {
    "ROLLBACK_WORKFLOW",
    "CANCEL_PRE_DELIVERY",
    "VOID_PRE_DELIVERY",
    "CANCEL_TIANHUA_PRE_DELIVERY",
    "VOID_TIANHUA_PRE_DELIVERY",
}


def _latest_predelivery_invalidations(
    db: Session,
    order_ids: list[int],
) -> dict[int, datetime]:
    rows = db.execute(
        select(OperationLog.entity_id, func.max(OperationLog.created_at))
        .where(
            OperationLog.entity_type == "order",
            OperationLog.entity_id.in_(order_ids),
            OperationLog.action.in_(PREDELIVERY_INVALIDATION_ACTIONS),
        )
        .group_by(OperationLog.entity_id)
    ).all()
    return {
        int(order_id): created_at
        for order_id, created_at in rows
        if order_id is not None
    }


def _active_predelivery_order_ids(db: Session, order_ids: list[int]) -> set[int]:
    invalidated_at = _latest_predelivery_invalidations(db, order_ids)
    rows = db.execute(
        select(TianhuaPreDeliveryDraftItem, TianhuaPreDeliveryDraft)
        .join(
            TianhuaPreDeliveryDraft,
            TianhuaPreDeliveryDraft.id == TianhuaPreDeliveryDraftItem.draft_id,
        )
        .where(TianhuaPreDeliveryDraftItem.order_id.in_(order_ids))
    ).all()
    active: set[int] = set()
    for item, draft in rows:
        rollback_at = invalidated_at.get(item.order_id)
        activity_at = draft.updated_at or item.created_at
        if rollback_at is None or activity_at is None or activity_at > rollback_at:
            active.add(item.order_id)
    return active


def _unlink_predelivery_order_bindings(
    db: Session,
    *,
    order_ids: list[int],
    reason: str,
) -> None:
    draft_items = db.scalars(
        select(TianhuaPreDeliveryDraftItem).where(
            TianhuaPreDeliveryDraftItem.order_id.in_(order_ids)
        )
    ).all()
    for item in draft_items:
        db.delete(item)

    import_items = db.scalars(
        select(TianhuaPreDeliveryImportItem).where(
            TianhuaPreDeliveryImportItem.order_id.in_(order_ids)
        )
    ).all()
    for item in import_items:
        item.order_item_id = None
        item.order_id = None
        item.order_number = None
        item.selected = False
        item.status = "not_matched"
        item.warning = reason


def _order_flow_dependencies(db: Session, order_ids: list[int]) -> list[str]:
    labels: list[str] = []
    if db.scalar(
        select(func.count())
        .select_from(DeliveryItem)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(OrderItem.order_id.in_(order_ids))
    ):
        labels.append("送货")
    if db.scalar(
        select(func.count())
        .select_from(RequisitionItem)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(OrderItem.order_id.in_(order_ids))
    ):
        labels.append("报料")
    if db.scalar(
        select(func.count())
        .select_from(SupplierRequisitionOrderItem)
        .join(OrderItem, OrderItem.id == SupplierRequisitionOrderItem.order_item_id)
        .where(OrderItem.order_id.in_(order_ids))
    ):
        labels.append("供应商采购单")

    if _active_predelivery_order_ids(db, order_ids):
        labels.append("有效预送货")
    return labels


def _flow_delete_message(labels: list[str]) -> str:
    if "有效预送货" in labels:
        return "该订单仍存在有效预送货流程，请先撤回或作废预送货后再删除。"
    flow_text = "/".join(dict.fromkeys(labels))
    return f"该订单已进入{flow_text}流程，不能直接删除。"


def _release_order_reservations(
    db: Session,
    *,
    order_item_ids: list[int],
    operator_id: int | None,
    reason: str,
    idempotency_prefix: str,
    allow_downstream: bool = False,
) -> None:
    try:
        release_active_finished_reservations_for_items(
            db,
            order_item_ids=order_item_ids,
            operator_id=operator_id,
            reason=reason,
            idempotency_prefix=idempotency_prefix,
            allow_downstream=allow_downstream,
        )
    except WarehouseInventoryError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error


def _delete_orders_in_transaction(
    db: Session,
    *,
    orders: list[Order],
    user: User,
) -> None:
    dependencies = _order_flow_dependencies(db, [order.id for order in orders])
    if dependencies:
        raise HTTPException(status_code=409, detail=_flow_delete_message(dependencies))
    item_ids = [item.id for order in orders for item in order.items]
    _unlink_predelivery_order_bindings(
        db,
        order_ids=[order.id for order in orders],
        reason="原订单已撤回或删除，历史预送货绑定已解除。",
    )
    _release_order_reservations(
        db,
        order_item_ids=item_ids,
        operator_id=user.id,
        reason="删除订单前自动释放成品库存预占",
        idempotency_prefix="delete-order-reservation",
    )
    for order in orders:
        db.add(
            OperationLog(
                user_id=user.id,
                action="DELETE",
                resource="Order",
                details=json.dumps(
                    {
                        "order_number": order.order_number,
                        "customer_id": order.customer_id,
                        "customer_po": order.customer_po,
                        "item_count": len(order.items),
                    },
                    ensure_ascii=False,
                ),
                username=user.username,
                role=user.role,
                entity_type="order",
                entity_id=order.id,
                description="删除无业务关联订单",
            )
        )
        db.delete(order)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="订单存在关联业务记录，不能直接删除。请刷新页面后检查报料、送货或对账状态。",
        ) from error


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
        _release_order_reservations(
            db,
            order_item_ids=[item.id for item in order.items],
            operator_id=user.id,
            reason=f"订单状态变更为{target}，自动释放成品库存预占",
            idempotency_prefix=f"order-status-{order.id}-{target}",
        )
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
    _delete_orders_in_transaction(db, orders=[order], user=user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/group-delete")
def delete_order_group(
    payload: OrderGroupDeleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="删除订单组需要二次确认")
    order_ids = list(dict.fromkeys(payload.order_ids))
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id.in_(order_ids))
        .order_by(Order.id)
    ).all()
    if len(orders) != len(order_ids):
        raise HTTPException(status_code=404, detail="订单组中有订单不存在，请刷新后重试")
    if len({_order_group_key(order) for order in orders}) != 1:
        raise HTTPException(status_code=400, detail="所选订单不属于同一订单组，请刷新后重试")
    _delete_orders_in_transaction(db, orders=orders, user=user)
    return {"deleted_count": len(orders)}


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
        _unlink_predelivery_order_bindings(
            db,
            order_ids=[order.id],
            reason="订单流程已撤回，历史预送货绑定已解除。",
        )
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
        _release_order_reservations(
            db,
            order_item_ids=item_ids,
            operator_id=user.id,
            reason="订单流程撤回，自动释放成品库存预占",
            idempotency_prefix=f"rollback-order-{order.id}",
            allow_downstream=True,
        )
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
        .options(
            selectinload(Order.items).selectinload(OrderItem.product).selectinload(
                Product.drawings  # type: ignore[attr-defined]
            )
        )
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
    if payload.import_integrity_status == "failed":
        errors = payload.import_integrity_errors or []
        message = (
            "当前 PDF 识别存在漏行或合计不一致，不能直接保存。"
            "请先人工补齐或确认异常。"
        )
        if errors:
            message = f"{message} {'；'.join(errors)}"
        raise HTTPException(status_code=400, detail=message)

    try:
        customer = db.get(Customer, payload.customer_id)
        if customer is None:
            raise HTTPException(status_code=400, detail="客户不存在")

        customer_po = (payload.customer_po or "").strip() or None

        new_product_cache: dict[str, Product] = {}
        resolved_products: dict[int, Product] = {}
        validated_quantities: dict[int, int] = {}
        for index, item_payload in enumerate(payload.items, start=1):
            validated_quantities[index] = _validated_order_quantity(
                item_payload.quantity,
                index,
            )
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
                    validated_quantities[index],
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
            quantity = validated_quantities[index]

            subtotal = (
                Decimal(quantity) * unit_price
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
                quantity=quantity,
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
                snapshot_customer_model=(
                    (item_payload.customer_model or "").strip() or None
                ),  # v0.19.1: TH型号 / 客户型号
                snapshot_production_notes=(
                    (item_payload.production_notes or "").strip() or None
                ),  # v0.19.2-A: 生产/印刷说明
                # v0.19.2-B: 常用箱层数/楞型/材质/供应商/克重 — 优先前端传值，否则从product取
                layer_count=(
                    item_payload.layer_count
                    or product.layer_count
                ),
                flute_type=(
                    (item_payload.flute_type or "").strip().upper() or product.flute_type or None
                ),
                material_id=(
                    item_payload.material_id or product.material_id
                ),
                snapshot_supplier_name=(
                    product.material.supplier_name if product.material is not None else None
                ),
                snapshot_weight=(
                    product.material.basis_weight_description if product.material is not None else None
                ),
                # v0.19.2-B: 报料快照（从常用箱复制，历史不回填）
                snapshot_report_length_mm=product.report_length_mm,
                snapshot_report_width_mm=product.report_width_mm,
                snapshot_crease_type=product.crease_type,
                snapshot_crease_left_mm=product.crease_left_mm,
                snapshot_crease_middle_mm=product.crease_middle_mm,
                snapshot_crease_right_mm=product.crease_right_mm,
                snapshot_report_notes=product.report_notes,
                snapshot_base_report_length_mm=product.base_report_length_mm,
                snapshot_base_report_width_mm=product.base_report_width_mm,
                snapshot_base_crease_type=product.base_crease_type,
                snapshot_base_crease_left_mm=product.base_crease_left_mm,
                snapshot_base_crease_middle_mm=product.base_crease_middle_mm,
                snapshot_base_crease_right_mm=product.base_crease_right_mm,
                snapshot_base_report_notes=product.base_report_notes,
                snapshot_splice_mode=product.splice_mode or "single",
                snapshot_pieces_per_box=product.pieces_per_box or (2 if (product.splice_mode or "").lower() == "double" else 1),
                snapshot_flap_mm=product.flap_mm or 30,
                requisition_status="未报料",
            )
            # v0.19.2-B: 临时图纸路径 — 新建订单前上传的图纸绑定到明细
            if item_payload.temp_drawing_file:
                import os, uuid, shutil
                tmp_path = item_payload.temp_drawing_file.lstrip("/")
                if os.path.isfile(tmp_path):
                    ext = tmp_path.rsplit(".", 1)[-1].lower() or "png"
                    fname = f"{uuid.uuid4().hex}.{ext}"
                    draw_dir = "static/uploads/drawings"
                    os.makedirs(draw_dir, exist_ok=True)
                    dest = f"{draw_dir}/{fname}"
                    shutil.copy2(tmp_path, dest)
                    item.drawing_file = f"/static/uploads/drawings/{fname}"
                else:
                    # 路径不合法时直接使用原路径（保底）
                    item.drawing_file = item_payload.temp_drawing_file
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
        db.flush()  # 获取 item.id 以便处理图纸
        # v0.19.2-B: 图纸保存到常用箱
        for i, item in enumerate(created_items):
            opt = payload.items[i].drawing_save_option if i < len(payload.items) else None
            if item.drawing_file and item.product_id and opt in ("save_to_product", "overwrite_product"):
                from app.models.product_drawing import ProductDrawing
                if opt == "overwrite_product":
                    from sqlalchemy import delete as _del
                    db.execute(_del(ProductDrawing).where(ProductDrawing.product_id == item.product_id))
                db.add(ProductDrawing(
                    product_id=item.product_id,
                    image_path=item.drawing_file,
                    thumbnail_path=item.drawing_file,
                    uploaded_by=user.id,
                ))
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
    finished_reserved_qty = active_finished_reserved_qty(db, item.id)
    if payload.quantity < finished_reserved_qty:
        raise HTTPException(
            status_code=409,
            detail=(
                f"订单数量不能小于已预占成品库存 {finished_reserved_qty}，"
                "请先取消成品库存抵扣"
            ),
        )
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
    if payload.production_notes is not None:
        item.snapshot_production_notes = (
            payload.production_notes.strip() or None
        )
    # v0.19.2-B: 材质联动字段
    if payload.material_id is not None:
        item.material_id = payload.material_id
        mat = db.get(Material, payload.material_id)
        if mat is not None:
            item.snapshot_supplier_name = mat.supplier_name
            item.snapshot_weight = mat.basis_weight_description
    if payload.layer_count is not None:
        item.layer_count = payload.layer_count
    if payload.flute_type is not None:
        item.flute_type = (payload.flute_type or "").strip().upper() or None
    # v0.19.2-B: 报料快照
    if payload.snapshot_report_length_mm is not None:
        item.snapshot_report_length_mm = payload.snapshot_report_length_mm
    if payload.snapshot_report_width_mm is not None:
        item.snapshot_report_width_mm = payload.snapshot_report_width_mm
    if payload.snapshot_crease_type is not None:
        item.snapshot_crease_type = payload.snapshot_crease_type or None
    if payload.snapshot_crease_left_mm is not None:
        item.snapshot_crease_left_mm = payload.snapshot_crease_left_mm
    if payload.snapshot_crease_middle_mm is not None:
        item.snapshot_crease_middle_mm = payload.snapshot_crease_middle_mm
    if payload.snapshot_crease_right_mm is not None:
        item.snapshot_crease_right_mm = payload.snapshot_crease_right_mm
    if payload.snapshot_report_notes is not None:
        item.snapshot_report_notes = payload.snapshot_report_notes or None
    if payload.snapshot_base_report_length_mm is not None:
        item.snapshot_base_report_length_mm = payload.snapshot_base_report_length_mm
    if payload.snapshot_base_report_width_mm is not None:
        item.snapshot_base_report_width_mm = payload.snapshot_base_report_width_mm
    if payload.snapshot_base_crease_type is not None:
        item.snapshot_base_crease_type = payload.snapshot_base_crease_type or None
    if payload.snapshot_base_crease_left_mm is not None:
        item.snapshot_base_crease_left_mm = payload.snapshot_base_crease_left_mm
    if payload.snapshot_base_crease_middle_mm is not None:
        item.snapshot_base_crease_middle_mm = payload.snapshot_base_crease_middle_mm
    if payload.snapshot_base_crease_right_mm is not None:
        item.snapshot_base_crease_right_mm = payload.snapshot_base_crease_right_mm
    if payload.snapshot_base_report_notes is not None:
        item.snapshot_base_report_notes = payload.snapshot_base_report_notes or None
    if payload.snapshot_splice_mode is not None:
        item.snapshot_splice_mode = payload.snapshot_splice_mode or None
    if payload.snapshot_pieces_per_box is not None:
        item.snapshot_pieces_per_box = payload.snapshot_pieces_per_box
    if payload.snapshot_flap_mm is not None:
        item.snapshot_flap_mm = payload.snapshot_flap_mm
    effective_box_style = payload.box_style or (item.product.box_style if item.product else None)
    if payload.box_style is not None:
        if not _box_style_uses_splice(effective_box_style):
            item.snapshot_splice_mode = "single"
            item.snapshot_pieces_per_box = 1
        if not _box_style_uses_tongue(effective_box_style):
            item.snapshot_flap_mm = None
    if payload.sync_product and item.product_id:
        product = db.get(Product, item.product_id)
        if product is None:
            raise HTTPException(status_code=409, detail="关联常用箱不存在，订单明细未保存")
        if payload.material_id is not None:
            product.material_id = payload.material_id
        if payload.layer_count is not None:
            product.layer_count = payload.layer_count
        if payload.flute_type is not None:
            product.flute_type = (payload.flute_type or "").strip().upper() or None
        for field_name in (
            "box_style",
            "length_mm",
            "width_mm",
            "height_mm",
            "production_process",
            "print_content",
        ):
            value = getattr(payload, field_name)
            if value is not None:
                setattr(product, field_name, value)
        product.splice_mode = payload.snapshot_splice_mode or product.splice_mode or "single"
        product.pieces_per_box = (
            payload.snapshot_pieces_per_box
            if payload.snapshot_pieces_per_box is not None
            else (2 if product.splice_mode == "double" else 1)
        )
        if payload.snapshot_flap_mm is not None:
            product.flap_mm = payload.snapshot_flap_mm
        if payload.snapshot_report_length_mm is not None:
            product.report_length_mm = payload.snapshot_report_length_mm
        if payload.snapshot_report_width_mm is not None:
            product.report_width_mm = payload.snapshot_report_width_mm
        if payload.snapshot_crease_type is not None:
            product.crease_type = payload.snapshot_crease_type or None
        if payload.snapshot_crease_left_mm is not None:
            product.crease_left_mm = payload.snapshot_crease_left_mm
        if payload.snapshot_crease_middle_mm is not None:
            product.crease_middle_mm = payload.snapshot_crease_middle_mm
        if payload.snapshot_crease_right_mm is not None:
            product.crease_right_mm = payload.snapshot_crease_right_mm
        if payload.snapshot_report_notes is not None:
            product.report_notes = payload.snapshot_report_notes or None
        if payload.snapshot_base_report_length_mm is not None:
            product.base_report_length_mm = payload.snapshot_base_report_length_mm
        if payload.snapshot_base_report_width_mm is not None:
            product.base_report_width_mm = payload.snapshot_base_report_width_mm
        if payload.snapshot_base_crease_type is not None:
            product.base_crease_type = payload.snapshot_base_crease_type or None
        if payload.snapshot_base_crease_left_mm is not None:
            product.base_crease_left_mm = payload.snapshot_base_crease_left_mm
        if payload.snapshot_base_crease_middle_mm is not None:
            product.base_crease_middle_mm = payload.snapshot_base_crease_middle_mm
        if payload.snapshot_base_crease_right_mm is not None:
            product.base_crease_right_mm = payload.snapshot_base_crease_right_mm
        if payload.snapshot_base_report_notes is not None:
            product.base_report_notes = payload.snapshot_base_report_notes or None
        if payload.box_style is not None:
            if not _box_style_uses_splice(product.box_style):
                product.splice_mode = "single"
                product.pieces_per_box = 1
            if not _box_style_uses_tongue(product.box_style):
                product.flap_mm = None
        if payload.product_remark is not None:
            product.remark = payload.product_remark.strip() or None
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
        "snapshot_customer_model": item.snapshot_customer_model,  # v0.19.1
        "snapshot_production_notes": item.snapshot_production_notes,  # v0.19.2-A
        "snapshot_report_length_mm": item.snapshot_report_length_mm,
        "snapshot_report_width_mm": item.snapshot_report_width_mm,
        "snapshot_crease_type": item.snapshot_crease_type,
        "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
        "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
        "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
        "snapshot_report_notes": item.snapshot_report_notes,
        "snapshot_base_report_length_mm": item.snapshot_base_report_length_mm,
        "snapshot_base_report_width_mm": item.snapshot_base_report_width_mm,
        "snapshot_base_crease_type": item.snapshot_base_crease_type,
        "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
        "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
        "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
        "snapshot_base_report_notes": item.snapshot_base_report_notes,
        "snapshot_splice_mode": item.snapshot_splice_mode,
        "snapshot_pieces_per_box": item.snapshot_pieces_per_box,
        "snapshot_flap_mm": item.snapshot_flap_mm,
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
    _release_order_reservations(
        db,
        order_item_ids=[item.id],
        operator_id=user.id,
        reason="删除订单明细前自动释放成品库存预占",
        idempotency_prefix=f"delete-order-item-{item.id}",
    )
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
