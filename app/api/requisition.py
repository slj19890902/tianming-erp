from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.material import Material
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
CUTTING_MODE_FACTORS = {
    "一开一": 1,
    "一开二": 2,
    "一开三": 3,
    "一开四": 4,
    "一开五": 5,
}
DEFAULT_CUTTING_MODE = "一开一"
SPECIAL_PROCESSES = {"无", "大做小", "双拼", "多拼"}
SUPPLIER_MATERIAL_FLUTES = {"AB", "E", "BE", "B", "C", "A"}


def _clean_supplier_material_code(value: str | None, layer_count: int | None) -> str:
    """清洗供应商报料材质代码，去掉楞型和历史组合尾巴。"""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    tokens = re.findall(r"[A-Z0-9]+", raw)
    candidates = [token for token in tokens if any(char.isalpha() for char in token)]
    if not candidates:
        return ""
    # AB/BE、B/E 等只有楞型的组合不是材质代码。
    if len(candidates) > 1 and all(token in SUPPLIER_MATERIAL_FLUTES for token in candidates):
        return ""
    code = candidates[0]
    expected_length = 5 if layer_count == 5 else 3 if layer_count == 3 else None
    return code[:expected_length] if expected_length else code


def _clean_supplier_flute(value: str | None) -> str:
    flute = re.sub(r"\s+", "", str(value or "").strip().upper())
    return flute if flute in SUPPLIER_MATERIAL_FLUTES else ""


def _format_supplier_material(
    material_code: str | None,
    layer_count: int | None,
    flute_type: str | None,
    fallback_text: str | None = None,
) -> str:
    """供应商采购报料单材质统一显示为“材质代码 / 楞型”."""
    code = _clean_supplier_material_code(material_code, layer_count)
    if not code and fallback_text:
        code = _clean_supplier_material_code(fallback_text, layer_count)
        if not code:
            code = str(fallback_text).strip()
    flute = _clean_supplier_flute(flute_type)
    if code and flute:
        return f"{code} / {flute}"
    return code or flute


def _company_sender(db: Session) -> dict:
    company = db.get(CompanyConfig, 1)
    return {
        "company_name": company.company_name if company else "",
        "address": company.address if company else None,
        "phone": company.phone if company else None,
    }


class RequisitionLinePayload(BaseModel):
    order_item_id: int
    inventory_deducted_qty: int = Field(default=0, ge=0)
    requisition_qty: int | None = Field(default=None, ge=0)
    cardboard_len: Decimal = Field(gt=0)
    cardboard_width: Decimal = Field(gt=0)
    special_process: str = DEFAULT_CUTTING_MODE
    remark: str | None = None

    @field_validator("special_process")
    @classmethod
    def validate_process(cls, value: str) -> str:
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


class PendingMaterialUpdate(BaseModel):
    material_id: int
    layer_count: int | None = None
    flute_type: str | None = None
    sync_product: bool = True

    @field_validator("flute_type")
    @classmethod
    def normalize_flute(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().upper()
        return normalized or None


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
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


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


def _pieces_per_box(item: OrderItem) -> int:
    value = item.snapshot_pieces_per_box
    if value in (1, 2):
        return value
    if (item.snapshot_splice_mode or "").strip().lower() == "double":
        return 2
    return 1


def _cutting_factor(cutting_mode: str | None) -> int:
    return CUTTING_MODE_FACTORS.get((cutting_mode or "").strip(), 1)


def _required_piece_qty(order_qty: int, pieces_per_box: int) -> int:
    return max(int(order_qty or 0), 0) * max(int(pieces_per_box or 1), 1)


def _purchase_qty(required_piece_qty: int, inventory_deducted_qty: int, cutting_mode: str | None) -> int:
    remaining = max(int(required_piece_qty or 0) - max(int(inventory_deducted_qty or 0), 0), 0)
    factor = _cutting_factor(cutting_mode)
    return (remaining + factor - 1) // factor


def _purchase_dimensions(
    report_length_mm: int | None,
    report_width_mm: int | None,
    cutting_mode: str | None,
) -> tuple[Decimal | None, Decimal | None]:
    if not report_length_mm or not report_width_mm:
        return None, None
    factor = _cutting_factor(cutting_mode)
    return Decimal(report_length_mm), Decimal(report_width_mm * factor)


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
    pieces_per_box = _pieces_per_box(item)
    cutting_mode = item.special_process if item.special_process in CUTTING_MODE_FACTORS else DEFAULT_CUTTING_MODE
    required_piece_qty = _required_piece_qty(item.quantity, pieces_per_box)
    return {
        "item_id": item.id,
        "inventory_deducted_qty": item.inventory_deducted_qty,
        "requisition_qty": item.requisition_qty,
        "requisition_status": item.requisition_status,
        "special_process": item.special_process,
        "cutting_mode": cutting_mode,
        "cutting_factor": _cutting_factor(cutting_mode),
        "pieces_per_box": pieces_per_box,
        "required_piece_qty": required_piece_qty,
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
        .order_by(OrderItem.created_at.desc(), OrderItem.id.desc())
    ).all()
    items = []
    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        material = db.get(Material, item.material_id) if item.material_id else None
        pieces_per_box = _pieces_per_box(item)
        cutting_mode = item.special_process if item.special_process in CUTTING_MODE_FACTORS else DEFAULT_CUTTING_MODE
        required_piece_qty = _required_piece_qty(item.quantity, pieces_per_box)
        suggested_len, suggested_width = _purchase_dimensions(
            item.snapshot_report_length_mm,
            item.snapshot_report_width_mm,
            DEFAULT_CUTTING_MODE,
        )
        if suggested_len is None or suggested_width is None:
            suggested_len, suggested_width = _suggested_dimensions(product)
        items.append(
            {
                "item_id": item.id,
                "order_number": display_order_number(order, registry),
                "display_order_number": display_order_number(order, registry),
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_id": product.id,
                "product_code": item.snapshot_product_code or product.product_code,
                "product_name": item.snapshot_product_name,
                "specification": item.snapshot_spec,
                "material": item.snapshot_material,
                "material_display": _format_supplier_material(
                    material.code if material else item.snapshot_material,
                    item.layer_count or (material.layer_count if material else None),
                    item.flute_type or (material.flute_type if material else None),
                    fallback_text=item.snapshot_material,
                ),
                "quantity": item.quantity,
                "delivery_date": order.delivery_date,
                "inventory_deducted_qty": item.inventory_deducted_qty,
                "requisition_qty": (
                    item.requisition_qty
                    if item.requisition_qty is not None
                    else _purchase_qty(required_piece_qty, item.inventory_deducted_qty, cutting_mode)
                ),
                "requisition_status": item.requisition_status,
                "special_process": item.special_process,
                "cutting_mode": cutting_mode,
                "cutting_factor": _cutting_factor(cutting_mode),
                "pieces_per_box": pieces_per_box,
                "required_piece_qty": required_piece_qty,
                "suggested_cardboard_len": item.cardboard_len or suggested_len,
                "suggested_cardboard_width": item.cardboard_width or suggested_width,
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
                "snapshot_splice_mode": item.snapshot_splice_mode,
                "snapshot_pieces_per_box": item.snapshot_pieces_per_box,
                "snapshot_flap_mm": item.snapshot_flap_mm,
            }
        )
    supplier_counts: dict[str, int] = {}
    for row in items:
        supplier = (row.get("snapshot_supplier_name") or "未设置供应商").strip()
        supplier_counts[supplier] = supplier_counts.get(supplier, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "supplier_counts": [
            {"supplier_name": supplier, "count": count}
            for supplier, count in sorted(
                supplier_counts.items(), key=lambda entry: (-entry[1], entry[0])
            )
        ],
    }


@router.put("/pending/{item_id}/material")
def update_pending_material(
    item_id: int,
    payload: PendingMaterialUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    item = _item_or_404(db, item_id)
    if item.requisition_status != "未报料":
        raise HTTPException(status_code=409, detail="已生成报料单的明细不能更换供应商或材质")
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细不能更换供应商或材质")
    material = db.get(Material, payload.material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=404, detail="所选材质不存在或已停用")
    layer_count = payload.layer_count or material.layer_count
    flute_type = payload.flute_type or item.flute_type
    if layer_count == 3 and flute_type not in {"A", "B", "E"}:
        raise HTTPException(status_code=400, detail="三层纸板楞型只能选择 A、B 或 E")
    if layer_count == 5 and flute_type not in {"AB", "BE"}:
        raise HTTPException(status_code=400, detail="五层纸板楞型只能选择 AB 或 BE")
    before = {
        "material_id": item.material_id,
        "supplier_name": item.snapshot_supplier_name,
        "layer_count": item.layer_count,
        "flute_type": item.flute_type,
    }
    item.material_id = material.id
    item.snapshot_material = material.code
    item.snapshot_supplier_name = material.supplier_name
    item.snapshot_weight = material.basis_weight_description
    item.layer_count = layer_count
    item.flute_type = flute_type
    if payload.sync_product and item.product_id:
        product = db.get(Product, item.product_id)
        if product is not None:
            product.material_id = material.id
            product.layer_count = layer_count
            product.flute_type = flute_type
    _audit(
        db,
        user=user,
        action="UPDATE_PENDING_MATERIAL",
        entity_id=item.id,
        details={
            "before": before,
            "after": {
                "material_id": material.id,
                "supplier_name": material.supplier_name,
                "layer_count": layer_count,
                "flute_type": flute_type,
                "sync_product": payload.sync_product,
            },
        },
        description="更换未报料明细供应商和材质",
    )
    db.commit()
    return {
        "item_id": item.id,
        "material_id": material.id,
        "material_code": material.code,
        "supplier_name": material.supplier_name,
        "layer_count": layer_count,
        "flute_type": flute_type,
        "message": (
            f"已将该明细改为 {material.supplier_name or '未设置供应商'} / "
            f"{material.code} / {flute_type or '-'}"
        ),
    }


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
            OrderItem.created_at.desc(),
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
            pieces_per_box = _pieces_per_box(item)
            required_piece_qty = _required_piece_qty(item.quantity, pieces_per_box)
            if line.inventory_deducted_qty > required_piece_qty:
                raise HTTPException(status_code=400, detail="库存抵扣数不能超过需求小片数")
            requisition_qty = (
                line.requisition_qty
                if line.requisition_qty is not None
                else _purchase_qty(required_piece_qty, line.inventory_deducted_qty, line.special_process)
            )
            if requisition_qty < 0:
                raise HTTPException(status_code=400, detail="采购报料张数不能为负数")
            spec = f"{_plain(line.cardboard_len)}?{_plain(line.cardboard_width)}"
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
                pieces_per_box=pieces_per_box,
                required_piece_qty=required_piece_qty,
                special_process=line.special_process,
                material_snapshot=item.snapshot_material,
                product_code_snapshot=(item.snapshot_product_code or product.product_code),
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
    pieces_per_box = _pieces_per_box(item)
    required_piece_qty = _required_piece_qty(item.quantity, pieces_per_box)
    if payload.inventory_deducted_qty > required_piece_qty:
        raise HTTPException(status_code=400, detail="库存抵扣数不能超过需求小片数")
    item.inventory_deducted_qty = payload.inventory_deducted_qty
    item.requisition_qty = payload.requisition_qty
    item.cardboard_len = payload.cardboard_len
    item.cardboard_width = payload.cardboard_width
    item.requisition_spec = f"{_plain(payload.cardboard_len)}?{_plain(payload.cardboard_width)}"
    item.special_process = payload.special_process
    item.requisition_remark = (payload.remark or "").strip() or None
    db.execute(
        update(RequisitionItem)
        .where(
            RequisitionItem.order_item_id == item.id,
            RequisitionItem.status == "有效",
        )
        .values(
            inventory_deducted_qty=payload.inventory_deducted_qty,
            requisition_qty=payload.requisition_qty,
            cardboard_len=payload.cardboard_len,
            cardboard_width=payload.cardboard_width,
            pieces_per_box=pieces_per_box,
            required_piece_qty=required_piece_qty,
            special_process=payload.special_process,
            remark=item.requisition_remark,
        )
    )
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
        raise HTTPException(status_code=409, detail="已入库明细禁止修改报料")
    if item.requisition_status == "未报料":
        raise HTTPException(status_code=409, detail="订单明细已经报料")
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
    item.special_process = DEFAULT_CUTTING_MODE
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
        description="修改报料信息",
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
        .order_by(OrderItem.created_at.desc(), OrderItem.id.desc())
    ).all()

    def _merge_key(item: OrderItem) -> tuple:
        return (
            item.snapshot_supplier_name or "",
            item.material_id or 0,
            item.layer_count or 0,
            (item.flute_type or "").upper(),
            item.snapshot_report_length_mm or 0,
            item.snapshot_report_width_mm or 0,
            _pieces_per_box(item),
            (item.snapshot_splice_mode or "single"),
            item.snapshot_crease_type or "",
            item.snapshot_crease_left_mm or 0,
            item.snapshot_crease_middle_mm or 0,
            item.snapshot_crease_right_mm or 0,
        )

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        if not (item.snapshot_report_length_mm and item.snapshot_report_width_mm):
            continue
        material = db.get(Material, item.material_id) if item.material_id else None
        pieces_per_box = _pieces_per_box(item)
        groups[_merge_key(item)].append({
            "item_id": item.id,
            "order_number": display_order_number(order, registry),
            "customer_name": customer.name,
            "product_code": item.snapshot_product_code or product.product_code,
            "product_name": item.snapshot_product_name,
            "specification": item.snapshot_spec,
            "material_id": item.material_id,
            "material_display": _format_supplier_material(
                material.code if material else item.snapshot_material,
                item.layer_count or (material.layer_count if material else None),
                item.flute_type or (material.flute_type if material else None),
                fallback_text=item.snapshot_material,
            ),
            "snapshot_supplier_name": item.snapshot_supplier_name,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "quantity": item.quantity,
            "pieces_per_box": pieces_per_box,
            "required_piece_qty": _required_piece_qty(item.quantity, pieces_per_box),
            "requisition_qty": _purchase_qty(
                _required_piece_qty(item.quantity, pieces_per_box),
                item.inventory_deducted_qty,
                DEFAULT_CUTTING_MODE,
            ),
            "delivery_date": order.delivery_date,
        })

    suggestions = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        supplier_name, material_id, layer_count, flute_type, report_len, report_width, pieces_per_box, splice_mode, crease_type, crease_left, crease_middle, crease_right = key
        material = db.get(Material, material_id) if material_id else None
        crease_display = (
            f"{crease_left}+{crease_middle}+{crease_right}"
            if crease_type == "压线" and crease_middle
            else crease_type or "-"
        )
        suggestions.append({
            "key": str(key),
            "supplier_name": supplier_name,
            "material_id": material_id,
            "material_display": _format_supplier_material(
                material.code if material else None,
                layer_count or (material.layer_count if material else None),
                flute_type or (material.flute_type if material else None),
                fallback_text=material.paper_composition if material else None,
            ),
            "layer_count": layer_count,
            "flute_type": flute_type,
            "report_length_mm": report_len,
            "report_width_mm": report_width,
            "pieces_per_box": pieces_per_box,
            "splice_mode": splice_mode,
            "cutting_mode": DEFAULT_CUTTING_MODE,
            "crease_type": crease_type,
            "crease_left_mm": crease_left,
            "crease_middle_mm": crease_middle,
            "crease_right_mm": crease_right,
            "crease_display": crease_display,
            "total_quantity": sum(m["quantity"] for m in members),
            "total_required_piece_qty": sum(m["required_piece_qty"] for m in members),
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
            OrderItem,
            Material,
        )
        .outerjoin(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .outerjoin(Material, Material.id == OrderItem.material_id)
        .where(
            RequisitionItem.requisition_id == batch.id,
            RequisitionItem.status == "有效",
        )
        .order_by(RequisitionItem.id)
    ).all()
    print_items = []
    for row, order_item, material in rows:
        order_layer_count = order_item.layer_count if order_item else None
        order_flute_type = order_item.flute_type if order_item else None
        material_code = material.code if material else None
        material_layer_count = material.layer_count if material else None
        material_flute_type = material.flute_type if material else None
        crease_type = order_item.snapshot_crease_type if order_item else None
        if crease_type == "压线" and order_item and order_item.snapshot_crease_middle_mm is not None:
            crease_display = (
                f"{order_item.snapshot_crease_left_mm or 0}+"
                f"{order_item.snapshot_crease_middle_mm}+"
                f"{order_item.snapshot_crease_right_mm or 0}"
            )
        elif crease_type == "毛片":
            crease_display = "毛"
        elif crease_type == "净料":
            crease_display = "净"
        else:
            crease_display = crease_type or ""
        remarks = []
        if row.special_process and row.special_process != DEFAULT_CUTTING_MODE:
            remarks.append(row.special_process)
        if row.remark:
            remarks.append(row.remark)
        if order_item and order_item.snapshot_report_notes:
            remarks.append(order_item.snapshot_report_notes)
        print_items.append(
            {
                "product_code": row.product_code_snapshot,
                "product_name": row.product_name_snapshot,
                "material": _format_supplier_material(
                    material_code or row.material_snapshot,
                    order_layer_count or material_layer_count,
                    order_flute_type or material_flute_type,
                    fallback_text=row.material_snapshot,
                ),
                "material_code": _clean_supplier_material_code(
                    material_code or row.material_snapshot,
                    order_layer_count or material_layer_count,
                ),
                "flute_type": _clean_supplier_flute(
                    order_flute_type or material_flute_type
                ),
                "specification": f"{_plain(row.cardboard_len)}×{_plain(row.cardboard_width)}",
                "crease_display": crease_display,
                "quantity": row.requisition_qty,
                "special_process": row.special_process,
                "cutting_mode": row.special_process,
                "production_notes": (
                    order_item.snapshot_production_notes if order_item else None
                ),
                "report_remark": "；".join(dict.fromkeys(filter(None, remarks))),
            }
        )
    return {
        "id": batch.id,
        "requisition_number": batch.requisition_number,
        "requisition_date": batch.requisition_date,
        "supplier_name": batch.supplier_name,
        "sender": _company_sender(db),
        "items": print_items,
        "total_quantity": sum(row.requisition_qty for row, *_ in rows),
    }


class SupplierOrderMemberPayload(BaseModel):
    item_id: int | None = None
    stock_deduction_qty: int = Field(default=0, ge=0)
    requisition_qty: int | None = Field(default=None, ge=0)
    order_number: str | None = None
    product_code: str | None = None
    product_name: str | None = None
    quantity: int = 0
    pieces_per_box: int | None = None
    required_piece_qty: int | None = None
    cutting_mode: str = DEFAULT_CUTTING_MODE
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
    cutting_mode: str = DEFAULT_CUTTING_MODE
    pieces_per_box: int | None = None
    required_piece_qty: int | None = None
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


def _supplier_order_dict(order: SupplierRequisitionOrder, db: Session) -> dict:
    material = db.get(Material, order.material_id) if order.material_id else None
    material_code = material.code if material else None
    material_layer_count = order.layer_count or (material.layer_count if material else None)
    material_flute_type = order.flute_type or (material.flute_type if material else None)
    crease_display = (
        f"{order.crease_left_mm}+{order.crease_middle_mm}+{order.crease_right_mm}"
        if order.crease_type == "压线" and order.crease_middle_mm
        else order.crease_type or "-"
    )
    return {
        "id": order.id,
        "order_number": order.order_number,
        "supplier_name": order.supplier_name,
        "sender": _company_sender(db),
        "material_id": order.material_id,
        "material_code": _clean_supplier_material_code(material_code, material_layer_count),
        "material_display": _format_supplier_material(
            material_code,
            material_layer_count,
            material_flute_type,
            fallback_text=material.paper_composition if material else None,
        ),
        "layer_count": order.layer_count,
        "flute_type": _clean_supplier_flute(material_flute_type),
        "report_length_mm": order.report_length_mm,
        "report_width_mm": order.report_width_mm,
        "crease_type": order.crease_type,
        "crease_left_mm": order.crease_left_mm,
        "crease_middle_mm": order.crease_middle_mm,
        "crease_right_mm": order.crease_right_mm,
        "crease_display": crease_display,
        "cutting_mode": order.cutting_mode or DEFAULT_CUTTING_MODE,
        "cutting_factor": _cutting_factor(order.cutting_mode),
        "pieces_per_box": order.pieces_per_box,
        "required_piece_qty": order.required_piece_qty,
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
                "cutting_mode": item.cutting_mode or order.cutting_mode or DEFAULT_CUTTING_MODE,
                "pieces_per_box": item.pieces_per_box,
                "required_piece_qty": item.required_piece_qty,
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
    if not payload.members:
        raise HTTPException(status_code=400, detail="至少需要一条明细")

    order_number = _supplier_order_number(db)
    total_qty = sum(m.quantity for m in payload.members)
    total_deduct = sum(m.stock_deduction_qty for m in payload.members)
    total_req = sum(
        m.requisition_qty if m.requisition_qty is not None else max(m.quantity - m.stock_deduction_qty, 0)
        for m in payload.members
    )
    total_required_piece_qty = sum(int(m.required_piece_qty or 0) for m in payload.members)

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
        cutting_mode=payload.cutting_mode,
        pieces_per_box=payload.pieces_per_box,
        required_piece_qty=payload.required_piece_qty if payload.required_piece_qty is not None else total_required_piece_qty,
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
            cutting_mode=m.cutting_mode or payload.cutting_mode,
            pieces_per_box=m.pieces_per_box,
            required_piece_qty=m.required_piece_qty,
            customer_name=m.customer_name,
            delivery_date=date.fromisoformat(m.delivery_date) if m.delivery_date else None,
        ))
        oi = db.get(OrderItem, m.item_id) if m.item_id else None
        if oi:
            oi.requisition_status = "已报料"
            oi.inventory_deducted_qty = m.stock_deduction_qty
            oi.requisition_qty = req_qty
            oi.special_process = m.cutting_mode or payload.cutting_mode

    db.commit()
    db.refresh(order)
    return _supplier_order_dict(order, db)


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
        "items": [_supplier_order_dict(o, db) for o in orders],
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
    return _supplier_order_dict(order, db)


@router.put("/supplier-orders/{order_id}/void")
def void_supplier_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
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
                oi.special_process = DEFAULT_CUTTING_MODE

    db.commit()
    db.refresh(order)
    return _supplier_order_dict(order, db)
