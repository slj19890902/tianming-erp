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
from app.services.flute_mapping import validate_flute_consistency
from app.services.warehouse_inventory import (
    active_finished_reserved_qty,
    active_finished_reservations_by_item_ids,
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
    component_type: str | None = None
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

    @field_validator("component_type")
    @classmethod
    def validate_component_type(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"cover", "base"}:
            raise ValueError("天地盖组件仅允许 cover 或 base")
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
        seen_single: set[int] = set()
        seen_components: set[tuple[int, str]] = set()
        for item in value:
            if item.component_type:
                key = (item.order_item_id, item.component_type)
                if key in seen_components:
                    raise ValueError("同一天地盖组件不能重复报料")
                seen_components.add(key)
                continue
            if item.order_item_id in seen_single:
                raise ValueError("同一订单明细不能重复报料")
            seen_single.add(item.order_item_id)
        if seen_single & {order_item_id for order_item_id, _ in seen_components}:
            raise ValueError("同一订单明细不能同时按整单和组件报料")
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


class MergeGroupCreatePayload(BaseModel):
    member_item_ids: list[int] = Field(min_length=2)
    supplier_name: str | None = None
    report_length_mm: Decimal = Field(gt=0)
    report_width_mm: Decimal = Field(gt=0)
    cutting_mode: str = DEFAULT_CUTTING_MODE
    remark: str | None = None

    @field_validator("member_item_ids")
    @classmethod
    def validate_member_item_ids(cls, value: list[int]) -> list[int]:
        unique_ids = list(dict.fromkeys(int(item_id) for item_id in value))
        if len(unique_ids) < 2:
            raise ValueError("至少选择两条待报料明细才能确认合并")
        return unique_ids

    @field_validator("cutting_mode")
    @classmethod
    def validate_cutting_mode(cls, value: str) -> str:
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


class MergeGroupUpdatePayload(BaseModel):
    supplier_name: str | None = None
    report_length_mm: Decimal | None = Field(default=None, gt=0)
    report_width_mm: Decimal | None = Field(default=None, gt=0)
    cutting_mode: str | None = None
    remark: str | None = None

    @field_validator("cutting_mode")
    @classmethod
    def validate_cutting_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


class PendingSupplierOrderSelection(BaseModel):
    type: str
    order_item_id: int | None = None
    merge_group_id: int | None = None
    supplier_name: str | None = None
    report_length_mm: Decimal | None = Field(default=None, gt=0)
    report_width_mm: Decimal | None = Field(default=None, gt=0)
    cutting_mode: str = DEFAULT_CUTTING_MODE
    remark: str | None = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in {"order_item", "merge_group"}:
            raise ValueError("待报料选择类型仅允许 order_item 或 merge_group")
        return normalized

    @field_validator("cutting_mode")
    @classmethod
    def validate_cutting_mode(cls, value: str) -> str:
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


class PendingSupplierOrderCreatePayload(BaseModel):
    selections: list[PendingSupplierOrderSelection] = Field(min_length=1)


class PendingSupplierOrderDraftItem(BaseModel):
    source_type: str
    order_item_id: int
    merge_group_id: int | None = None
    report_length_mm: Decimal = Field(gt=0)
    report_width_mm: Decimal = Field(gt=0)
    cutting_mode: str = DEFAULT_CUTTING_MODE
    inventory_deducted_qty: int = 0
    requisition_qty: int
    remark: str | None = None

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in {"order_item", "merge_group_item"}:
            raise ValueError("报料草稿来源类型仅允许 order_item 或 merge_group_item")
        return normalized

    @field_validator("cutting_mode")
    @classmethod
    def validate_cutting_mode(cls, value: str) -> str:
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


class PendingSupplierOrderDraftSourceItem(BaseModel):
    source_type: str
    order_item_id: int
    merge_group_id: int | None = None
    source_quantity: int | None = None
    inventory_deducted_qty: int | None = None
    requisition_qty: int | None = None

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in {"order_item", "merge_group_item"}:
            raise ValueError("采购草稿来源类型仅允许 order_item 或 merge_group_item")
        return normalized


class PendingSupplierOrderDraftLine(BaseModel):
    line_key: str | None = None
    source_type: str | None = None
    report_length_mm: Decimal = Field(gt=0)
    report_width_mm: Decimal = Field(gt=0)
    cutting_mode: str = DEFAULT_CUTTING_MODE
    inventory_deducted_qty: int = 0
    requisition_qty: int
    remark: str | None = None
    source_items: list[PendingSupplierOrderDraftSourceItem] = Field(min_length=1)

    @field_validator("cutting_mode")
    @classmethod
    def validate_cutting_mode(cls, value: str) -> str:
        normalized = str(value or "").strip() or DEFAULT_CUTTING_MODE
        if normalized not in CUTTING_MODE_FACTORS:
            raise ValueError("开料方式仅允许：一开一、一开二、一开三、一开四、一开五")
        return normalized


class PendingSupplierOrderDraftGroup(BaseModel):
    supplier_name: str | None = None
    lines: list[PendingSupplierOrderDraftLine] = Field(default_factory=list)
    # Backward-compatible input for the previous flat source-item draft shape.
    items: list[dict] = Field(default_factory=list)


class PendingSupplierOrderFinalizePayload(BaseModel):
    supplier_groups: list[PendingSupplierOrderDraftGroup] = Field(min_length=1)


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pieces_per_box(item: OrderItem) -> int:
    value = item.snapshot_pieces_per_box
    if value in (1, 2):
        return value
    if (item.snapshot_splice_mode or "").strip().lower() == "double":
        return 2
    return 1


def _is_telescoping_lid_box(box_style: str | None) -> bool:
    value = (box_style or "").strip().upper()
    return bool(value) and ("天地盖" in value or "A3" in value)


def _component_crease(item: OrderItem, component: str | None) -> tuple[str | None, int | None, int | None, int | None]:
    if component == "base":
        return (
            item.snapshot_base_crease_type,
            item.snapshot_base_crease_left_mm,
            item.snapshot_base_crease_middle_mm,
            item.snapshot_base_crease_right_mm,
        )
    return (
        item.snapshot_crease_type,
        item.snapshot_crease_left_mm,
        item.snapshot_crease_middle_mm,
        item.snapshot_crease_right_mm,
    )


def _cutting_factor(cutting_mode: str | None) -> int:
    return CUTTING_MODE_FACTORS.get((cutting_mode or "").strip(), 1)


def _required_piece_qty(order_qty: int, pieces_per_box: int) -> int:
    return max(int(order_qty or 0), 0) * max(int(pieces_per_box or 1), 1)


def _purchase_qty(required_piece_qty: int, inventory_deducted_qty: int, cutting_mode: str | None) -> int:
    remaining = max(int(required_piece_qty or 0) - max(int(inventory_deducted_qty or 0), 0), 0)
    factor = _cutting_factor(cutting_mode)
    return (remaining + factor - 1) // factor


def _current_requisition_requirements(
    db: Session,
    item: OrderItem,
    *,
    cutting_mode: str | None = None,
    pieces_per_box: int | None = None,
    finished_reserved_qty: int | None = None,
) -> dict[str, int | str | bool]:
    """Derive current purchase demand from active finished-stock reservations."""
    resolved_cutting_mode = cutting_mode or item.special_process
    if resolved_cutting_mode not in CUTTING_MODE_FACTORS:
        resolved_cutting_mode = DEFAULT_CUTTING_MODE
    resolved_pieces_per_box = max(
        int(
            pieces_per_box
            if pieces_per_box is not None
            else _pieces_per_box(item)
        ),
        1,
    )
    resolved_reserved_qty = max(
        int(
            finished_reserved_qty
            if finished_reserved_qty is not None
            else active_finished_reserved_qty(db, item.id)
        ),
        0,
    )
    production_required_qty = max(
        int(item.quantity or 0) - resolved_reserved_qty,
        0,
    )
    required_piece_qty = _required_piece_qty(
        production_required_qty,
        resolved_pieces_per_box,
    )
    return {
        "cutting_mode": resolved_cutting_mode,
        "cutting_factor": _cutting_factor(resolved_cutting_mode),
        "pieces_per_box": resolved_pieces_per_box,
        "finished_inventory_reserved_qty": resolved_reserved_qty,
        "production_required_qty": production_required_qty,
        "fully_covered_by_finished_inventory": production_required_qty == 0,
        "required_piece_qty": required_piece_qty,
        "requisition_qty": _purchase_qty(
            required_piece_qty,
            0,
            resolved_cutting_mode,
        ),
    }


def _purchase_dimensions(
    report_length_mm: int | None,
    report_width_mm: int | None,
    cutting_mode: str | None,
) -> tuple[Decimal | None, Decimal | None]:
    if not report_length_mm or not report_width_mm:
        return None, None
    factor = _cutting_factor(cutting_mode)
    return Decimal(report_length_mm), Decimal(report_width_mm * factor)


def _supplier_dimension_warnings(
    supplier_name: str | None,
    cardboard_len: Decimal | int | None,
    cardboard_width: Decimal | int | None,
    cutting_mode: str | None = None,
) -> list[str]:
    if "嘉林亿" not in (supplier_name or ""):
        return []
    warnings = []
    length = Decimal(cardboard_len or 0)
    width = Decimal(cardboard_width or 0)
    if length and length < 500:
        warnings.append(
            f"采购长 {_plain(length)}mm 低于供应商最小切长 500mm，请调整报料尺寸或人工确认。"
        )
    if width and width < 270:
        message = (
            f"采购宽 {_plain(width)}mm 低于供应商最小切宽 270mm，建议调整开料方式。"
        )
        if _cutting_factor(cutting_mode) == 1:
            doubled = width * 2
            message += f"建议改为一开二后采购宽 = {_plain(doubled)}mm"
            message += "，满足最小切宽。" if doubled >= 270 else "，仍低于最小切宽。"
        warnings.append(message)
    return warnings


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


def _item_response(item: OrderItem, db: Session | None = None) -> dict:
    pieces_per_box = _pieces_per_box(item)
    cutting_mode = item.special_process if item.special_process in CUTTING_MODE_FACTORS else DEFAULT_CUTTING_MODE
    finished_reserved_qty = (
        active_finished_reserved_qty(db, item.id) if db is not None else 0
    )
    production_required_qty = max(item.quantity - finished_reserved_qty, 0)
    required_piece_qty = _required_piece_qty(
        production_required_qty, pieces_per_box
    )
    return {
        "item_id": item.id,
        "inventory_deducted_qty": 0,
        "legacy_inventory_deducted_qty": item.inventory_deducted_qty,
        "finished_inventory_reserved_qty": finished_reserved_qty,
        "production_required_qty": production_required_qty,
        "fully_covered_by_finished_inventory": production_required_qty == 0,
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


def _unique_text(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text_value = str(value or "").strip()
        if not text_value or text_value in seen:
            continue
        seen.add(text_value)
        result.append(text_value)
    return result


def _merge_group_rows(db: Session, group_id: int) -> list[tuple[RequisitionItem, OrderItem, Order, Customer, Product]]:
    return db.execute(
        select(RequisitionItem, OrderItem, Order, Customer, Product)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(RequisitionItem.requisition_id == group_id)
        .order_by(RequisitionItem.id)
    ).all()


def _merge_group_dict(
    group: Requisition,
    db: Session,
    *,
    display_registry=None,
) -> dict:
    rows = _merge_group_rows(db, group.id)
    registry = display_registry or build_display_registry(db)
    members = []
    product_codes: list[str | None] = []
    order_numbers: list[str | None] = []
    customer_names: list[str | None] = []
    product_names: list[str | None] = []
    total_quantity = 0
    total_finished_reserved_qty = 0
    total_production_required_qty = 0
    total_required_piece_qty = 0
    total_requisition_qty = 0
    first_item: OrderItem | None = None
    first_req_item: RequisitionItem | None = None
    first_material: Material | None = None
    for req_item, order_item, order, customer, product in rows:
        if first_item is None:
            first_item = order_item
            first_req_item = req_item
            first_material = db.get(Material, order_item.material_id) if order_item.material_id else None
        display_no = display_order_number(order, registry)
        product_code = req_item.product_code_snapshot or order_item.snapshot_product_code or product.product_code
        product_name = req_item.product_name_snapshot or order_item.snapshot_product_name
        requirements = _current_requisition_requirements(
            db,
            order_item,
            cutting_mode=req_item.special_process,
            pieces_per_box=req_item.pieces_per_box or _pieces_per_box(order_item),
        )
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        required_piece_qty = int(requirements["required_piece_qty"])
        requisition_qty = int(requirements["requisition_qty"])
        product_codes.append(product_code)
        order_numbers.append(display_no)
        customer_names.append(customer.name)
        product_names.append(product_name)
        total_quantity += int(order_item.quantity or 0)
        total_finished_reserved_qty += finished_reserved_qty
        total_production_required_qty += production_required_qty
        total_required_piece_qty += required_piece_qty
        total_requisition_qty += requisition_qty
        members.append(
            {
                "item_id": order_item.id,
                "requisition_item_id": req_item.id,
                "order_number": display_no,
                "display_order_number": display_no,
                "customer_name": customer.name,
                "product_code": product_code,
                "product_name": product_name,
                "specification": req_item.specification_snapshot or order_item.snapshot_spec,
                "quantity": order_item.quantity,
                "finished_inventory_reserved_qty": finished_reserved_qty,
                "production_required_qty": production_required_qty,
                "fully_covered_by_finished_inventory": production_required_qty == 0,
                "requisition_qty": requisition_qty,
                "required_piece_qty": required_piece_qty,
                "pieces_per_box": int(requirements["pieces_per_box"]),
                "cutting_factor": int(requirements["cutting_factor"]),
                "cardboard_len": req_item.cardboard_len,
                "cardboard_width": req_item.cardboard_width,
                "cutting_mode": str(requirements["cutting_mode"]),
                "special_process": str(requirements["cutting_mode"]),
                "delivery_date": order.delivery_date,
            }
        )
    material_display = ""
    if first_item is not None:
        material_display = _format_supplier_material(
            first_material.code if first_material else first_item.snapshot_material,
            first_item.layer_count or (first_material.layer_count if first_material else None),
            first_item.flute_type,
            fallback_text=first_item.snapshot_material,
        )
    supplier_name = (group.supplier_name or "").strip()
    cardboard_len = first_req_item.cardboard_len if first_req_item else None
    cardboard_width = first_req_item.cardboard_width if first_req_item else None
    cutting_mode = first_req_item.special_process if first_req_item else DEFAULT_CUTTING_MODE
    return {
        "item_id": f"mg{group.id}",
        "id": group.id,
        "is_merge_group": True,
        "merge_group_id": group.id,
        "status": group.status,
        "order_number": "合并组",
        "display_order_number": "合并组",
        "order_numbers": _unique_text(order_numbers),
        "customer_name": " / ".join(_unique_text(customer_names)),
        "customer_names": _unique_text(customer_names),
        "product_code": " / ".join(_unique_text(product_codes)),
        "product_codes": _unique_text(product_codes),
        "product_name": " / ".join(_unique_text(product_names)),
        "product_names": _unique_text(product_names),
        "specification": f"{len(members)} 个来源",
        "quantity": total_quantity,
        "finished_inventory_reserved_qty": total_finished_reserved_qty,
        "production_required_qty": total_production_required_qty,
        "fully_covered_by_finished_inventory": total_production_required_qty == 0,
        "requisition_qty": total_requisition_qty,
        "requisition_status": "待报料合并组",
        "required_piece_qty": total_required_piece_qty,
        "total_quantity": total_quantity,
        "total_required_piece_qty": total_required_piece_qty,
        "suggested_cardboard_len": cardboard_len,
        "suggested_cardboard_width": cardboard_width,
        "report_length_mm": cardboard_len,
        "report_width_mm": cardboard_width,
        "cardboard_len": cardboard_len,
        "cardboard_width": cardboard_width,
        "cutting_mode": cutting_mode,
        "special_process": cutting_mode,
        "remark": first_req_item.remark if first_req_item else None,
        "snapshot_supplier_name": supplier_name,
        "supplier_name": supplier_name,
        "material_display": material_display,
        "material": first_item.snapshot_material if first_item else None,
        "material_id": first_item.material_id if first_item else None,
        "layer_count": first_item.layer_count if first_item else None,
        "flute_type": first_item.flute_type if first_item else None,
        "members": members,
        "dimension_warnings": _supplier_dimension_warnings(
            supplier_name,
            cardboard_len,
            cardboard_width,
            cutting_mode,
        ),
    }


def _validate_merge_member_rows(
    db: Session,
    member_item_ids: list[int],
) -> list[tuple[OrderItem, Order, Customer, Product]]:
    rows = db.execute(
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(OrderItem.id.in_(member_item_ids))
    ).all()
    found_ids = {item.id for item, *_ in rows}
    missing = [item_id for item_id in member_item_ids if item_id not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"订单明细不存在：{missing}")
    existing_group_item = db.scalar(
        select(RequisitionItem)
        .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
        .where(
            RequisitionItem.order_item_id.in_(member_item_ids),
            Requisition.status == "merged_pending",
            RequisitionItem.status == "merged_pending",
        )
        .limit(1)
    )
    if existing_group_item is not None:
        raise HTTPException(status_code=409, detail="所选明细已属于待报料合并组，请勿重复合并")
    by_id = {item.id: (item, order, customer, product) for item, order, customer, product in rows}
    ordered_rows = [by_id[item_id] for item_id in member_item_ids]
    for item, order, *_ in ordered_rows:
        if is_history_order_number(order.order_number):
            raise HTTPException(status_code=409, detail="历史订单不能创建待报料合并组")
        if order.status in {"cancelled", "dead", "closed", "archived"}:
            raise HTTPException(status_code=409, detail="已取消、死单、结单或归档订单不能创建待报料合并组")
        if item.is_force_closed:
            raise HTTPException(status_code=409, detail="强制结案明细不能创建待报料合并组")
        if item.material_status != "pending":
            raise HTTPException(status_code=409, detail="已入库或非待生产明细不能创建待报料合并组")
        if item.requisition_status != "未报料":
            raise HTTPException(status_code=409, detail="已报料明细不能创建待报料合并组")
    return ordered_rows


def _active_supplier_order_item_exists(db: Session, order_item_id: int) -> bool:
    return (
        db.scalar(
            select(SupplierRequisitionOrderItem.id)
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .where(
                SupplierRequisitionOrderItem.order_item_id == order_item_id,
                SupplierRequisitionOrder.status != "voided",
            )
            .limit(1)
        )
        is not None
    )


def _ensure_pending_order_item_for_supplier_order(
    db: Session,
    order_item_id: int,
) -> tuple[OrderItem, Order, Customer, Product]:
    row = db.execute(
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(OrderItem.id == order_item_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    item, order, customer, product = row
    if is_history_order_number(order.order_number):
        raise HTTPException(status_code=409, detail="历史订单不能生成供应商报料单")
    if order.status in {"cancelled", "dead", "closed", "archived"}:
        raise HTTPException(status_code=409, detail="已取消、死单、结单或归档订单不能生成供应商报料单")
    if item.is_force_closed:
        raise HTTPException(status_code=409, detail="强制结档明细不能生成供应商报料单")
    if item.material_status != "pending":
        raise HTTPException(status_code=409, detail="已入库或非待生产明细不能生成供应商报料单")
    if item.requisition_status != "未报料":
        raise HTTPException(status_code=409, detail="订单明细已经报料")
    if _active_supplier_order_item_exists(db, item.id):
        raise HTTPException(status_code=409, detail="订单明细已经存在有效供应商报料单")
    return item, order, customer, product


def _order_item_in_merged_pending_group(db: Session, order_item_id: int) -> bool:
    return (
        db.scalar(
            select(RequisitionItem.id)
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(
                RequisitionItem.order_item_id == order_item_id,
                Requisition.status == "merged_pending",
                RequisitionItem.status == "merged_pending",
            )
            .limit(1)
        )
        is not None
    )


def _entry_supplier(selection: PendingSupplierOrderSelection, fallback: str | None) -> str:
    supplier_name = (selection.supplier_name or fallback or "").strip()
    if not supplier_name:
        raise HTTPException(status_code=400, detail="每条待报料选择必须有供应商")
    return supplier_name


def _entry_decimal(
    value: Decimal | None,
    fallback: Decimal | int | None,
    field_name: str,
) -> Decimal:
    chosen = value if value is not None else fallback
    if chosen is None:
        raise HTTPException(status_code=400, detail=f"缺少{field_name}")
    decimal_value = Decimal(str(chosen))
    if decimal_value <= 0:
        raise HTTPException(status_code=400, detail=f"{field_name}必须大于 0")
    return decimal_value


def _pending_selection_entries(
    db: Session,
    payload: PendingSupplierOrderCreatePayload,
) -> tuple[dict[str, list[dict]], list[Requisition]]:
    grouped: dict[str, list[dict]] = {}
    touched_groups: list[Requisition] = []
    seen_order_item_ids: set[int] = set()
    seen_group_ids: set[int] = set()

    def add_entry(supplier_name: str, entry: dict) -> None:
        if entry["order_item"].id in seen_order_item_ids:
            raise HTTPException(status_code=409, detail="同一订单明细不能重复生成供应商报料单")
        seen_order_item_ids.add(entry["order_item"].id)
        grouped.setdefault(supplier_name, []).append(entry)

    for selection in payload.selections:
        if selection.type == "order_item":
            if not selection.order_item_id:
                raise HTTPException(status_code=400, detail="普通待报料行缺少 order_item_id")
            item, order, customer, product = _ensure_pending_order_item_for_supplier_order(
                db, selection.order_item_id
            )
            if _order_item_in_merged_pending_group(db, item.id):
                raise HTTPException(status_code=409, detail="订单明细已属于待报料合并组，不能按普通行重复报料")
            supplier_name = _entry_supplier(selection, item.snapshot_supplier_name)
            cardboard_len = _entry_decimal(
                selection.report_length_mm,
                item.cardboard_len or item.snapshot_report_length_mm,
                "报料长",
            )
            cardboard_width = _entry_decimal(
                selection.report_width_mm,
                item.cardboard_width or item.snapshot_report_width_mm,
                "报料宽",
            )
            cutting_mode = selection.cutting_mode or item.special_process or DEFAULT_CUTTING_MODE
            pieces_per_box = _pieces_per_box(item)
            finished_reserved_qty = active_finished_reserved_qty(db, item.id)
            production_required_qty = max(item.quantity - finished_reserved_qty, 0)
            if production_required_qty == 0:
                raise HTTPException(
                    status_code=409,
                    detail="该订单明细已由成品库存全额抵扣，无需生成供应商报料单",
                )
            required_piece_qty = _required_piece_qty(
                production_required_qty, pieces_per_box
            )
            add_entry(
                supplier_name,
                {
                    "source_type": "order_item",
                    "group": None,
                    "req_item": None,
                    "order_item": item,
                    "order": order,
                    "customer": customer,
                    "product": product,
                    "supplier_name": supplier_name,
                    "cardboard_len": cardboard_len,
                    "cardboard_width": cardboard_width,
                    "cutting_mode": cutting_mode,
                    "remark": (selection.remark or item.requisition_remark or "").strip() or None,
                    "inventory_deducted_qty": finished_reserved_qty,
                    "pieces_per_box": pieces_per_box,
                    "production_required_qty": production_required_qty,
                    "required_piece_qty": required_piece_qty,
                    "requisition_qty": _purchase_qty(
                        required_piece_qty, 0, cutting_mode
                    ),
                },
            )
            continue

        if not selection.merge_group_id:
            raise HTTPException(status_code=400, detail="合并组行缺少 merge_group_id")
        if selection.merge_group_id in seen_group_ids:
            raise HTTPException(status_code=409, detail="同一合并组不能重复生成供应商报料单")
        seen_group_ids.add(selection.merge_group_id)
        group = db.get(Requisition, selection.merge_group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="待报料合并组不存在")
        if group.status != "merged_pending":
            raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
        rows = _merge_group_rows(db, group.id)
        if not rows:
            raise HTTPException(status_code=400, detail="合并组没有来源明细")
        supplier_name = _entry_supplier(selection, group.supplier_name)
        first_req_item = rows[0][0]
        cardboard_len = _entry_decimal(
            selection.report_length_mm,
            first_req_item.cardboard_len,
            "报料长",
        )
        cardboard_width = _entry_decimal(
            selection.report_width_mm,
            first_req_item.cardboard_width,
            "报料宽",
        )
        cutting_mode = selection.cutting_mode or first_req_item.special_process or DEFAULT_CUTTING_MODE
        remark = (selection.remark or first_req_item.remark or "").strip() or None
        group.supplier_name = supplier_name
        for req_item, order_item, order, customer, product in rows:
            if req_item.status != "merged_pending":
                raise HTTPException(status_code=409, detail="合并组状态异常，不能生成供应商报料单")
            _ensure_pending_order_item_for_supplier_order(db, order_item.id)
            req_item.cardboard_len = cardboard_len
            req_item.cardboard_width = cardboard_width
            req_item.special_process = cutting_mode
            req_item.remark = remark
            requirements = _current_requisition_requirements(
                db,
                order_item,
                cutting_mode=cutting_mode,
                pieces_per_box=req_item.pieces_per_box or _pieces_per_box(order_item),
            )
            if bool(requirements["fully_covered_by_finished_inventory"]):
                raise HTTPException(
                    status_code=409,
                    detail="合并组中存在已由成品库存全额抵扣的明细，请刷新后重试",
                )
            req_item.pieces_per_box = int(requirements["pieces_per_box"])
            req_item.required_piece_qty = int(requirements["required_piece_qty"])
            req_item.requisition_qty = int(requirements["requisition_qty"])
            add_entry(
                supplier_name,
                {
                    "source_type": "merge_group",
                    "group": group,
                    "req_item": req_item,
                    "order_item": order_item,
                    "order": order,
                    "customer": customer,
                    "product": product,
                    "supplier_name": supplier_name,
                    "cardboard_len": cardboard_len,
                    "cardboard_width": cardboard_width,
                    "cutting_mode": cutting_mode,
                    "remark": remark,
                    "inventory_deducted_qty": int(
                        requirements["finished_inventory_reserved_qty"]
                    ),
                    "pieces_per_box": int(requirements["pieces_per_box"]),
                    "production_required_qty": int(
                        requirements["production_required_qty"]
                    ),
                    "required_piece_qty": int(requirements["required_piece_qty"]),
                    "requisition_qty": int(requirements["requisition_qty"]),
                },
            )
        touched_groups.append(group)

    return grouped, touched_groups


def _pending_entry_dict(entry: dict) -> dict:
    req_item: RequisitionItem | None = entry.get("req_item")
    order_item: OrderItem = entry["order_item"]
    order: Order = entry["order"]
    customer: Customer = entry["customer"]
    product: Product = entry["product"]
    material = db_material = entry.get("material")
    if material is None and order_item.material_id:
        db_material = None
    return {
        "source_type": entry["source_type"],
        "order_item_id": order_item.id,
        "merge_group_id": entry["group"].id if entry.get("group") is not None else None,
        "order_number": display_order_number(order, entry.get("display_registry") or {}),
        "customer_name": customer.name,
        "product_code": (
            req_item.product_code_snapshot
            if req_item is not None
            else order_item.snapshot_product_code or product.product_code
        ),
        "product_name": (
            req_item.product_name_snapshot
            if req_item is not None
            else order_item.snapshot_product_name
        ),
        "material_display": _format_supplier_material(
            db_material.code if db_material else order_item.snapshot_material,
            order_item.layer_count or (db_material.layer_count if db_material else None),
            order_item.flute_type,
            fallback_text=order_item.snapshot_material,
        ),
        "quantity": order_item.quantity,
        "report_length_mm": entry["cardboard_len"],
        "report_width_mm": entry["cardboard_width"],
        "cutting_mode": entry["cutting_mode"],
        "inventory_deducted_qty": entry.get("inventory_deducted_qty", 0),
        "finished_inventory_reserved_qty": entry.get(
            "inventory_deducted_qty", 0
        ),
        "production_required_qty": entry["production_required_qty"],
        "pieces_per_box": entry["pieces_per_box"],
        "required_piece_qty": entry["required_piece_qty"],
        "requisition_qty": entry["requisition_qty"],
        "remark": entry["remark"] or "",
    }


def _int_value(value, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


def _purchase_line_spec_from_entry(entry: dict) -> dict:
    order_item: OrderItem = entry["order_item"]
    material: Material | None = entry.get("material")
    material_id = order_item.material_id or (material.id if material else None)
    material_code = material.code if material else order_item.snapshot_material
    layer_count = order_item.layer_count or (material.layer_count if material else None)
    flute_type = _clean_supplier_flute(order_item.flute_type)
    clean_material_code = _clean_supplier_material_code(material_code, layer_count)
    return {
        "material_id": material_id,
        "material_code": clean_material_code,
        "material_display": _format_supplier_material(
            material_code,
            layer_count,
            flute_type,
            fallback_text=order_item.snapshot_material,
        ),
        "layer_count": layer_count,
        "flute_type": flute_type,
        "report_length_mm": _int_value(entry.get("cardboard_len")),
        "report_width_mm": _int_value(entry.get("cardboard_width")),
        "crease_type": order_item.snapshot_crease_type,
        "crease_left_mm": order_item.snapshot_crease_left_mm,
        "crease_middle_mm": order_item.snapshot_crease_middle_mm,
        "crease_right_mm": order_item.snapshot_crease_right_mm,
        "cutting_mode": entry.get("cutting_mode") or DEFAULT_CUTTING_MODE,
        "remark": entry.get("remark") or "",
    }


def _purchase_line_key(supplier_name: str | None, spec: dict) -> str:
    key_payload = {
        "supplier_name": (supplier_name or "").strip(),
        "material_id": spec.get("material_id"),
        "material_code": spec.get("material_code") or "",
        "material_display": spec.get("material_display") or "",
        "layer_count": spec.get("layer_count"),
        "flute_type": spec.get("flute_type") or "",
        "report_length_mm": spec.get("report_length_mm"),
        "report_width_mm": spec.get("report_width_mm"),
        "crease_type": spec.get("crease_type") or "",
        "crease_left_mm": spec.get("crease_left_mm"),
        "crease_middle_mm": spec.get("crease_middle_mm"),
        "crease_right_mm": spec.get("crease_right_mm"),
        "cutting_mode": spec.get("cutting_mode") or DEFAULT_CUTTING_MODE,
        "remark": spec.get("remark") or "",
    }
    return json.dumps(key_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_item_from_entry(entry: dict) -> dict:
    source = _pending_entry_dict(entry)
    source["source_quantity"] = int(entry.get("production_required_qty") or 0)
    source["required_piece_qty"] = int(entry.get("required_piece_qty") or 0)
    return source


def _aggregate_entries_to_purchase_lines(
    supplier_name: str | None,
    entries: list[dict],
) -> list[dict]:
    line_map: dict[str, dict] = {}
    for entry in entries:
        spec = _purchase_line_spec_from_entry(entry)
        line_key = _purchase_line_key(supplier_name, spec)
        line = line_map.get(line_key)
        if line is None:
            line = {
                "line_key": line_key,
                "source_type": "normal",
                **spec,
                "quantity": 0,
                "inventory_deducted_qty": 0,
                "finished_inventory_reserved_qty": 0,
                "production_required_qty": 0,
                "required_piece_qty": 0,
                "requisition_qty": 0,
                "source_items": [],
            }
            line_map[line_key] = line
        line["inventory_deducted_qty"] += int(entry.get("inventory_deducted_qty") or 0)
        line["finished_inventory_reserved_qty"] = line[
            "inventory_deducted_qty"
        ]
        line["production_required_qty"] += int(entry.get("production_required_qty") or 0)
        line["quantity"] = line["production_required_qty"]
        line["required_piece_qty"] += int(entry.get("required_piece_qty") or 0)
        line["requisition_qty"] += int(entry.get("requisition_qty") or 0)
        line["source_items"].append(_source_item_from_entry(entry))

    lines = list(line_map.values())
    for line in lines:
        source_types = {item.get("source_type") for item in line["source_items"]}
        if source_types == {"merge_group_item"}:
            line["source_type"] = "merge_group"
        elif source_types == {"order_item"}:
            line["source_type"] = "normal"
        else:
            line["source_type"] = "mixed"
    return lines


def _allocate_integer_total(total: int, weights: list[int]) -> list[int]:
    total = int(total or 0)
    if not weights:
        return []
    if len(weights) == 1:
        return [total]
    clean_weights = [max(int(w or 0), 0) for w in weights]
    weight_sum = sum(clean_weights)
    if weight_sum <= 0:
        result = [0 for _ in clean_weights]
        result[0] = total
        return result
    raw = [Decimal(total) * Decimal(weight) / Decimal(weight_sum) for weight in clean_weights]
    floors = [int(value) for value in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda idx: raw[idx] - floors[idx], reverse=True)
    for idx in order[:remainder]:
        floors[idx] += 1
    return floors


def _draft_lines_from_group(
    group_payload: PendingSupplierOrderDraftGroup,
) -> list[PendingSupplierOrderDraftLine]:
    if group_payload.lines:
        return group_payload.lines
    lines: list[PendingSupplierOrderDraftLine] = []
    for raw_item in group_payload.items:
        if "source_items" in raw_item:
            lines.append(PendingSupplierOrderDraftLine(**raw_item))
            continue
        item = PendingSupplierOrderDraftItem(**raw_item)
        lines.append(
            PendingSupplierOrderDraftLine(
                report_length_mm=item.report_length_mm,
                report_width_mm=item.report_width_mm,
                cutting_mode=item.cutting_mode,
                inventory_deducted_qty=item.inventory_deducted_qty,
                requisition_qty=item.requisition_qty,
                remark=item.remark,
                source_items=[
                    PendingSupplierOrderDraftSourceItem(
                        source_type=item.source_type,
                        order_item_id=item.order_item_id,
                        merge_group_id=item.merge_group_id,
                        inventory_deducted_qty=item.inventory_deducted_qty,
                        requisition_qty=item.requisition_qty,
                    )
                ],
            )
        )
    return lines


def _pending_selection_preview_groups(
    db: Session,
    payload: PendingSupplierOrderCreatePayload,
) -> dict:
    registry = build_display_registry(db)
    grouped: dict[str, list[dict]] = {}
    seen_order_item_ids: set[int] = set()
    seen_group_ids: set[int] = set()

    def add_preview(supplier_name: str, entry: dict) -> None:
        if entry["order_item"].id in seen_order_item_ids:
            raise HTTPException(status_code=409, detail="同一订单明细不能重复加入报料草稿")
        seen_order_item_ids.add(entry["order_item"].id)
        entry["display_registry"] = registry
        grouped.setdefault(supplier_name, []).append(entry)

    for selection in payload.selections:
        if selection.type == "order_item":
            if not selection.order_item_id:
                raise HTTPException(status_code=400, detail="普通待报料行缺少 order_item_id")
            item, order, customer, product = _ensure_pending_order_item_for_supplier_order(
                db, selection.order_item_id
            )
            if _order_item_in_merged_pending_group(db, item.id):
                raise HTTPException(status_code=409, detail="订单明细已属于待报料合并组，不能按普通行重复报料")
            supplier_name = (selection.supplier_name or item.snapshot_supplier_name or "").strip()
            cardboard_len = _entry_decimal(
                selection.report_length_mm,
                item.cardboard_len or item.snapshot_report_length_mm,
                "报料长",
            )
            cardboard_width = _entry_decimal(
                selection.report_width_mm,
                item.cardboard_width or item.snapshot_report_width_mm,
                "报料宽",
            )
            cutting_mode = selection.cutting_mode or item.special_process or DEFAULT_CUTTING_MODE
            requirements = _current_requisition_requirements(
                db,
                item,
                cutting_mode=cutting_mode,
            )
            inventory_deducted_qty = int(
                requirements["finished_inventory_reserved_qty"]
            )
            production_required_qty = int(
                requirements["production_required_qty"]
            )
            if production_required_qty == 0:
                raise HTTPException(
                    status_code=409,
                    detail="该订单明细已由成品库存全额抵扣，无需生成供应商报料单",
                )
            pieces_per_box = int(requirements["pieces_per_box"])
            required_piece_qty = int(requirements["required_piece_qty"])
            material = db.get(Material, item.material_id) if item.material_id else None
            add_preview(
                supplier_name,
                {
                    "source_type": "order_item",
                    "group": None,
                    "req_item": None,
                    "order_item": item,
                    "order": order,
                    "customer": customer,
                    "product": product,
                    "material": material,
                    "cardboard_len": cardboard_len,
                    "cardboard_width": cardboard_width,
                    "cutting_mode": cutting_mode,
                    "remark": (selection.remark or item.requisition_remark or "").strip() or None,
                    "inventory_deducted_qty": inventory_deducted_qty,
                    "pieces_per_box": pieces_per_box,
                    "production_required_qty": production_required_qty,
                    "required_piece_qty": required_piece_qty,
                    "requisition_qty": int(requirements["requisition_qty"]),
                },
            )
            continue

        if not selection.merge_group_id:
            raise HTTPException(status_code=400, detail="合并组行缺少 merge_group_id")
        if selection.merge_group_id in seen_group_ids:
            raise HTTPException(status_code=409, detail="同一合并组不能重复加入报料草稿")
        seen_group_ids.add(selection.merge_group_id)
        group = db.get(Requisition, selection.merge_group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="待报料合并组不存在")
        if group.status != "merged_pending":
            raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
        rows = _merge_group_rows(db, group.id)
        if not rows:
            raise HTTPException(status_code=400, detail="合并组没有来源明细")
        supplier_name = (selection.supplier_name or group.supplier_name or "").strip()
        for req_item, order_item, order, customer, product in rows:
            if req_item.status != "merged_pending":
                raise HTTPException(status_code=409, detail="合并组状态异常，不能生成报料草稿")
            _ensure_pending_order_item_for_supplier_order(db, order_item.id)
            material = db.get(Material, order_item.material_id) if order_item.material_id else None
            cutting_mode = selection.cutting_mode or req_item.special_process
            requirements = _current_requisition_requirements(
                db,
                order_item,
                cutting_mode=cutting_mode,
                pieces_per_box=req_item.pieces_per_box or _pieces_per_box(order_item),
            )
            if bool(requirements["fully_covered_by_finished_inventory"]):
                raise HTTPException(
                    status_code=409,
                    detail="合并组中存在已由成品库存全额抵扣的明细，请刷新后重试",
                )
            add_preview(
                supplier_name,
                {
                    "source_type": "merge_group_item",
                    "group": group,
                    "req_item": req_item,
                    "order_item": order_item,
                    "order": order,
                    "customer": customer,
                    "product": product,
                    "material": material,
                    "cardboard_len": selection.report_length_mm or req_item.cardboard_len,
                    "cardboard_width": selection.report_width_mm or req_item.cardboard_width,
                    "cutting_mode": requirements["cutting_mode"],
                    "remark": (selection.remark or req_item.remark or "").strip() or None,
                    "inventory_deducted_qty": int(
                        requirements["finished_inventory_reserved_qty"]
                    ),
                    "pieces_per_box": int(requirements["pieces_per_box"]),
                    "production_required_qty": int(
                        requirements["production_required_qty"]
                    ),
                    "required_piece_qty": int(requirements["required_piece_qty"]),
                    "requisition_qty": int(requirements["requisition_qty"]),
                },
            )

    supplier_groups = []
    for supplier_name, entries in grouped.items():
        lines = _aggregate_entries_to_purchase_lines(supplier_name, entries)
        supplier_groups.append(
            {
                "supplier_name": supplier_name,
                "lines": lines,
                # Compatibility alias. These are purchase-spec lines, not flat sources.
                "items": lines,
            }
        )
    return {"supplier_groups": supplier_groups}


def _draft_group_entries(
    db: Session,
    payload: PendingSupplierOrderFinalizePayload,
) -> tuple[dict[str, list[dict]], list[Requisition]]:
    grouped: dict[str, list[dict]] = {}
    touched_groups_by_id: dict[int, Requisition] = {}
    seen_order_item_ids: set[int] = set()

    for group_payload in payload.supplier_groups:
        supplier_name = (group_payload.supplier_name or "").strip()
        if not supplier_name:
            raise HTTPException(status_code=400, detail="每个供应商组必须选择供应商")
        for draft_item in group_payload.items:
            if draft_item.order_item_id in seen_order_item_ids:
                raise HTTPException(status_code=409, detail="同一订单明细不能重复生成供应商报料单")
            seen_order_item_ids.add(draft_item.order_item_id)
            item, order, customer, product = _ensure_pending_order_item_for_supplier_order(
                db, draft_item.order_item_id
            )
            req_item: RequisitionItem | None = None
            merge_group: Requisition | None = None
            if draft_item.source_type == "order_item":
                if _order_item_in_merged_pending_group(db, item.id):
                    raise HTTPException(status_code=409, detail="订单明细已属于待报料合并组，不能按普通行重复报料")
            else:
                if not draft_item.merge_group_id:
                    raise HTTPException(status_code=400, detail="合并组来源明细缺少 merge_group_id")
                merge_group = db.get(Requisition, draft_item.merge_group_id)
                if merge_group is None:
                    raise HTTPException(status_code=404, detail="待报料合并组不存在")
                if merge_group.status != "merged_pending":
                    raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
                req_item = db.scalar(
                    select(RequisitionItem).where(
                        RequisitionItem.requisition_id == merge_group.id,
                        RequisitionItem.order_item_id == item.id,
                    )
                )
                if req_item is None or req_item.status != "merged_pending":
                    raise HTTPException(status_code=409, detail="合并组来源明细状态异常")
                touched_groups_by_id[merge_group.id] = merge_group

            if draft_item.inventory_deducted_qty < 0:
                raise HTTPException(status_code=400, detail="成品库存抵扣不能小于 0")
            if draft_item.inventory_deducted_qty > item.quantity:
                raise HTTPException(status_code=400, detail="成品库存抵扣不能大于订单数量")
            if draft_item.requisition_qty <= 0:
                raise HTTPException(status_code=400, detail="采购张数必须大于 0")
            production_required_qty = item.quantity - draft_item.inventory_deducted_qty
            if production_required_qty <= 0:
                raise HTTPException(status_code=400, detail="成品库存抵扣后仍需报料数量必须大于 0")
            pieces_per_box = (
                req_item.pieces_per_box
                if req_item is not None and req_item.pieces_per_box
                else _pieces_per_box(item)
            )
            required_piece_qty = _required_piece_qty(production_required_qty, pieces_per_box)
            material = db.get(Material, item.material_id) if item.material_id else None
            entry = {
                "source_type": draft_item.source_type,
                "group": merge_group,
                "req_item": req_item,
                "order_item": item,
                "order": order,
                "customer": customer,
                "product": product,
                "material": material,
                "supplier_name": supplier_name,
                "cardboard_len": draft_item.report_length_mm,
                "cardboard_width": draft_item.report_width_mm,
                "cutting_mode": draft_item.cutting_mode,
                "remark": (draft_item.remark or "").strip() or None,
                "inventory_deducted_qty": draft_item.inventory_deducted_qty,
                "pieces_per_box": pieces_per_box,
                "production_required_qty": production_required_qty,
                "required_piece_qty": required_piece_qty,
                "requisition_qty": draft_item.requisition_qty,
            }
            if req_item is not None:
                req_item.cardboard_len = draft_item.report_length_mm
                req_item.cardboard_width = draft_item.report_width_mm
                req_item.special_process = draft_item.cutting_mode
                req_item.requisition_qty = draft_item.requisition_qty
                req_item.required_piece_qty = required_piece_qty
                req_item.remark = entry["remark"]
            grouped.setdefault(supplier_name, []).append(entry)

    return grouped, list(touched_groups_by_id.values())


def _draft_group_entries_by_purchase_lines(
    db: Session,
    payload: PendingSupplierOrderFinalizePayload,
) -> tuple[dict[str, list[dict]], list[Requisition]]:
    grouped: dict[str, list[dict]] = {}
    touched_groups_by_id: dict[int, Requisition] = {}
    seen_order_item_ids: set[int] = set()

    for group_payload in payload.supplier_groups:
        supplier_name = (group_payload.supplier_name or "").strip()
        if not supplier_name:
            raise HTTPException(status_code=400, detail="每个供应商组必须选择供应商")
        draft_lines = _draft_lines_from_group(group_payload)
        if not draft_lines:
            raise HTTPException(status_code=400, detail="每个供应商组必须至少包含一条采购规格行")

        for draft_line in draft_lines:
            if draft_line.requisition_qty <= 0:
                raise HTTPException(status_code=400, detail="采购张数必须大于 0")

            source_refs: list[dict] = []
            for source_payload in draft_line.source_items:
                if source_payload.order_item_id in seen_order_item_ids:
                    raise HTTPException(status_code=409, detail="同一订单明细不能重复生成供应商报料单")
                seen_order_item_ids.add(source_payload.order_item_id)
                item, order, customer, product = _ensure_pending_order_item_for_supplier_order(
                    db, source_payload.order_item_id
                )
                req_item: RequisitionItem | None = None
                merge_group: Requisition | None = None
                if source_payload.source_type == "order_item":
                    if _order_item_in_merged_pending_group(db, item.id):
                        raise HTTPException(status_code=409, detail="订单明细已属于待报料合并组，不能按普通行重复报料")
                else:
                    if not source_payload.merge_group_id:
                        raise HTTPException(status_code=400, detail="合并组来源明细缺少 merge_group_id")
                    merge_group = db.get(Requisition, source_payload.merge_group_id)
                    if merge_group is None:
                        raise HTTPException(status_code=404, detail="待报料合并组不存在")
                    if merge_group.status != "merged_pending":
                        raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
                    req_item = db.scalar(
                        select(RequisitionItem).where(
                            RequisitionItem.requisition_id == merge_group.id,
                            RequisitionItem.order_item_id == item.id,
                        )
                    )
                    if req_item is None or req_item.status != "merged_pending":
                        raise HTTPException(status_code=409, detail="合并组来源明细状态异常")
                    touched_groups_by_id[merge_group.id] = merge_group
                source_refs.append(
                    {
                        "source_payload": source_payload,
                        "item": item,
                        "order": order,
                        "customer": customer,
                        "product": product,
                        "req_item": req_item,
                        "merge_group": merge_group,
                    }
                )

            current_requirements = [
                _current_requisition_requirements(
                    db,
                    ref["item"],
                    cutting_mode=draft_line.cutting_mode,
                    pieces_per_box=(
                        ref["req_item"].pieces_per_box
                        if ref["req_item"] is not None
                        and ref["req_item"].pieces_per_box
                        else _pieces_per_box(ref["item"])
                    ),
                )
                for ref in source_refs
            ]
            actual_inventory_allocations = [
                int(requirements["finished_inventory_reserved_qty"])
                for requirements in current_requirements
            ]
            actual_inventory_total = sum(actual_inventory_allocations)
            submitted_line_total = int(draft_line.inventory_deducted_qty or 0)
            if submitted_line_total not in {0, actual_inventory_total}:
                raise HTTPException(
                    status_code=400,
                    detail="成品库存抵扣只能来自当前有效的真实库存预占，请刷新后重试",
                )
            for ref, actual_quantity in zip(
                source_refs, actual_inventory_allocations
            ):
                submitted_quantity = ref[
                    "source_payload"
                ].inventory_deducted_qty
                if submitted_quantity not in {None, 0, actual_quantity}:
                    raise HTTPException(
                        status_code=400,
                        detail="成品库存抵扣只能来自当前有效的真实库存预占，请刷新后重试",
                    )

            for ref, requirements in zip(
                source_refs, current_requirements
            ):
                item: OrderItem = ref["item"]
                order: Order = ref["order"]
                customer: Customer = ref["customer"]
                product: Product = ref["product"]
                req_item: RequisitionItem | None = ref["req_item"]
                merge_group: Requisition | None = ref["merge_group"]
                source_payload: PendingSupplierOrderDraftSourceItem = ref["source_payload"]
                inventory_deducted_qty = int(
                    requirements["finished_inventory_reserved_qty"]
                )
                requisition_qty = int(requirements["requisition_qty"])

                if inventory_deducted_qty < 0:
                    raise HTTPException(status_code=400, detail="成品库存抵扣不能小于 0")
                if inventory_deducted_qty > item.quantity:
                    raise HTTPException(status_code=400, detail="成品库存抵扣不能大于订单数量")
                if requisition_qty <= 0:
                    raise HTTPException(status_code=400, detail="采购张数必须大于 0")
                production_required_qty = int(
                    requirements["production_required_qty"]
                )
                if production_required_qty <= 0:
                    raise HTTPException(
                        status_code=409,
                        detail="该订单明细已由成品库存全额抵扣，无需生成供应商报料单",
                    )

                pieces_per_box = int(requirements["pieces_per_box"])
                required_piece_qty = int(requirements["required_piece_qty"])
                material = db.get(Material, item.material_id) if item.material_id else None
                entry = {
                    "source_type": source_payload.source_type,
                    "group": merge_group,
                    "req_item": req_item,
                    "order_item": item,
                    "order": order,
                    "customer": customer,
                    "product": product,
                    "material": material,
                    "supplier_name": supplier_name,
                    "cardboard_len": draft_line.report_length_mm,
                    "cardboard_width": draft_line.report_width_mm,
                    "cutting_mode": draft_line.cutting_mode,
                    "remark": (draft_line.remark or "").strip() or None,
                    "inventory_deducted_qty": inventory_deducted_qty,
                    "pieces_per_box": pieces_per_box,
                    "production_required_qty": production_required_qty,
                    "required_piece_qty": required_piece_qty,
                    "requisition_qty": requisition_qty,
                }
                if req_item is not None:
                    req_item.cardboard_len = draft_line.report_length_mm
                    req_item.cardboard_width = draft_line.report_width_mm
                    req_item.special_process = draft_line.cutting_mode
                    req_item.requisition_qty = requisition_qty
                    req_item.required_piece_qty = required_piece_qty
                    req_item.remark = entry["remark"]
                grouped.setdefault(supplier_name, []).append(entry)

    return grouped, list(touched_groups_by_id.values())


def _create_supplier_order_for_pending_entries(
    db: Session,
    *,
    supplier_name: str,
    entries: list[dict],
    user: User,
) -> SupplierRequisitionOrder:
    first = entries[0]
    first_item: OrderItem = first["order_item"]
    material = db.get(Material, first_item.material_id) if first_item.material_id else None
    order = SupplierRequisitionOrder(
        order_number=_supplier_order_number(db),
        supplier_name=supplier_name,
        material_id=first_item.material_id,
        layer_count=first_item.layer_count or (material.layer_count if material else None),
        flute_type=first_item.flute_type,
        report_length_mm=int(first["cardboard_len"]),
        report_width_mm=int(first["cardboard_width"]),
        crease_type=first_item.snapshot_crease_type,
        crease_left_mm=first_item.snapshot_crease_left_mm,
        crease_middle_mm=first_item.snapshot_crease_middle_mm,
        crease_right_mm=first_item.snapshot_crease_right_mm,
        cutting_mode=first["cutting_mode"],
        pieces_per_box=first["pieces_per_box"],
        required_piece_qty=sum(int(entry["required_piece_qty"] or 0) for entry in entries),
        total_quantity=sum(int(entry["production_required_qty"] or 0) for entry in entries),
        stock_deduction_qty=sum(int(entry.get("inventory_deducted_qty") or 0) for entry in entries),
        requisition_qty=sum(int(entry["requisition_qty"] or 0) for entry in entries),
        remark=first["remark"],
        status="confirmed",
        created_by=user.id,
    )
    db.add(order)
    db.flush()
    requisition_date = date.today()
    for entry in entries:
        order_item: OrderItem = entry["order_item"]
        req_item: RequisitionItem | None = entry["req_item"]
        db.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=order.id,
                order_item_id=order_item.id,
                order_number=order_item.item_order_number,
                product_code=(
                    req_item.product_code_snapshot
                    if req_item is not None
                    else order_item.snapshot_product_code or entry["product"].product_code
                ),
                product_name=(
                    req_item.product_name_snapshot
                    if req_item is not None
                    else order_item.snapshot_product_name
                ),
                quantity=int(entry["production_required_qty"] or 0),
                stock_deduction_qty=int(entry.get("inventory_deducted_qty") or 0),
                requisition_qty=int(entry["requisition_qty"] or 0),
                cutting_mode=entry["cutting_mode"],
                pieces_per_box=entry["pieces_per_box"],
                required_piece_qty=entry["required_piece_qty"],
                customer_name=entry["customer"].name,
                delivery_date=entry["order"].delivery_date,
            )
        )
        # The legacy free-entry field stays zero.  Real deductions are recorded
        # by InventoryReservation and copied only to supplier-order snapshots.
        order_item.inventory_deducted_qty = 0
        order_item.requisition_status = "已报料"
        order_item.requisition_qty = int(entry["requisition_qty"] or 0)
        order_item.special_process = entry["cutting_mode"]
        order_item.cardboard_len = entry["cardboard_len"]
        order_item.cardboard_width = entry["cardboard_width"]
        order_item.requisition_spec = (
            f"{_plain(entry['cardboard_len'])}×{_plain(entry['cardboard_width'])}"
        )
        order_item.requisition_date = requisition_date
        order_item.supplier_order_number = order.order_number
        order_item.requisition_remark = entry["remark"]
        if req_item is not None:
            req_item.status = "supplier_requisition_created"
    return order


@router.get("/pending")
def pending_requisitions(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    merge_groups = db.scalars(
        select(Requisition)
        .where(Requisition.status == "merged_pending")
        .order_by(Requisition.created_at.desc(), Requisition.id.desc())
    ).all()
    merged_order_item_ids = set(
        db.scalars(
            select(RequisitionItem.order_item_id)
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(
                Requisition.status == "merged_pending",
                RequisitionItem.status == "merged_pending",
            )
        ).all()
    )
    base_query = (
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            OrderItem.requisition_status == "未报料",
            OrderItem.material_status == "pending",
            Order.status.notin_(["cancelled", "dead", "closed", "archived"]),
            OrderItem.is_force_closed.is_(False),
        )
    )
    if merged_order_item_ids:
        base_query = base_query.where(~OrderItem.id.in_(merged_order_item_ids))
    rows = db.execute(
        base_query.order_by(OrderItem.created_at.desc(), OrderItem.id.desc())
    ).all()
    reservation_map = active_finished_reservations_by_item_ids(
        db, [item.id for item, *_ in rows]
    )
    items = []
    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        material = db.get(Material, item.material_id) if item.material_id else None
        requirements = _current_requisition_requirements(
            db,
            item,
            finished_reserved_qty=reservation_map.get(item.id, 0),
        )
        pieces_per_box = int(requirements["pieces_per_box"])
        cutting_mode = str(requirements["cutting_mode"])
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        required_piece_qty = int(requirements["required_piece_qty"])
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
                "is_merge_group": False,
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
                    item.flute_type,
                    fallback_text=item.snapshot_material,
                ),
                "quantity": item.quantity,
                "delivery_date": order.delivery_date,
                "inventory_deducted_qty": 0,
                "legacy_inventory_deducted_qty": item.inventory_deducted_qty,
                "finished_inventory_reserved_qty": finished_reserved_qty,
                "production_required_qty": production_required_qty,
                "fully_covered_by_finished_inventory": (
                    production_required_qty == 0
                ),
                "requisition_qty": (
                    0
                    if production_required_qty == 0
                    else int(requirements["requisition_qty"])
                ),
                "requisition_status": item.requisition_status,
                "special_process": item.special_process,
                "cutting_mode": cutting_mode,
                "cutting_factor": int(requirements["cutting_factor"]),
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
                "snapshot_base_report_length_mm": item.snapshot_base_report_length_mm,
                "snapshot_base_report_width_mm": item.snapshot_base_report_width_mm,
                "snapshot_base_crease_type": item.snapshot_base_crease_type,
                "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
                "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
                "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
                "snapshot_base_report_notes": item.snapshot_base_report_notes,
                "snapshot_splice_mode": item.snapshot_splice_mode,
                "snapshot_pieces_per_box": item.snapshot_pieces_per_box,
                "box_style": product.box_style,
                "snapshot_flap_mm": item.snapshot_flap_mm,
                "dimension_warnings": _supplier_dimension_warnings(
                    item.snapshot_supplier_name,
                    item.cardboard_len or suggested_len,
                    item.cardboard_width or suggested_width,
                    cutting_mode,
                ),
            }
        )
    items = [_merge_group_dict(group, db, display_registry=registry) for group in merge_groups] + items
    supplier_counts: dict[str, int] = {}
    for row in items:
        supplier = (row.get("supplier_name") or row.get("snapshot_supplier_name") or "未设置供应商").strip()
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
    flute_error = validate_flute_consistency(flute_type, layer_count)
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)
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
                **_item_response(item, db),
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
        lines_by_order_item: dict[int, list[RequisitionLinePayload]] = {}
        for line in payload.items:
            lines_by_order_item.setdefault(line.order_item_id, []).append(line)

        for order_item_id, lines in lines_by_order_item.items():
            row = db.execute(
                select(OrderItem, Product)
                .join(Product, Product.id == OrderItem.product_id)
                .where(OrderItem.id == order_item_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="订单明细不存在")
            item, product = row
            if item.material_status == "received":
                raise HTTPException(status_code=409, detail="已入库明细不能报料")
            if item.requisition_status != "未报料":
                raise HTTPException(status_code=409, detail="订单明细已经报料")
            pieces_per_box = _pieces_per_box(item)
            finished_reserved_qty = active_finished_reserved_qty(db, item.id)
            production_required_qty = max(
                item.quantity - finished_reserved_qty, 0
            )
            if production_required_qty == 0:
                raise HTTPException(
                    status_code=409,
                    detail="该订单明细已由成品库存全额抵扣，无需报料",
                )
            required_piece_qty = _required_piece_qty(
                production_required_qty, pieces_per_box
            )
            components = []
            for line in lines:
                if line.inventory_deducted_qty:
                    raise HTTPException(
                        status_code=400,
                        detail="旧库存抵扣字段已停用，请在订单明细中选择真实成品库存预占",
                    )
                calculated_qty = _purchase_qty(
                    required_piece_qty, 0, line.special_process
                )
                requisition_qty = (
                    int(line.requisition_qty)
                    if line.component_type and line.requisition_qty is not None
                    else calculated_qty
                )
                if requisition_qty < 0:
                    raise HTTPException(status_code=400, detail="采购报料张数不能为负数")
                if line.component_type:
                    is_base = line.component_type == "base"
                    components.append(
                        {
                            "kind": line.component_type,
                            "suffix": "底" if is_base else "盖",
                            "cardboard_len": line.cardboard_len,
                            "cardboard_width": line.cardboard_width,
                            "requisition_qty": requisition_qty,
                            "report_notes": (
                                item.snapshot_base_report_notes
                                if is_base
                                else item.snapshot_report_notes
                            ),
                            "special_process": line.special_process,
                        }
                    )
                    continue
                components.append(
                    {
                        "kind": "cover",
                        "suffix": "盖",
                        "cardboard_len": line.cardboard_len,
                        "cardboard_width": line.cardboard_width,
                        "requisition_qty": requisition_qty,
                        "report_notes": item.snapshot_report_notes,
                        "special_process": line.special_process,
                    }
                )
                if (
                    _is_telescoping_lid_box(product.box_style)
                    and item.snapshot_base_report_length_mm
                    and item.snapshot_base_report_width_mm
                ):
                    components.append(
                        {
                            "kind": "base",
                            "suffix": "底",
                            "cardboard_len": Decimal(item.snapshot_base_report_length_mm),
                            "cardboard_width": Decimal(item.snapshot_base_report_width_mm),
                            "requisition_qty": requisition_qty,
                            "report_notes": item.snapshot_base_report_notes,
                            "special_process": line.special_process,
                        }
                    )
            first_line = lines[0]
            total_requisition_qty = sum(
                int(component["requisition_qty"]) for component in components
            )
            if len(components) > 1:
                spec = "；".join(
                    f"{component['suffix']}:{_plain(component['cardboard_len'])}×{_plain(component['cardboard_width'])}"
                    for component in components
                )
            else:
                spec = f"{_plain(first_line.cardboard_len)}×{_plain(first_line.cardboard_width)}"
            item.inventory_deducted_qty = 0
            item.requisition_qty = total_requisition_qty
            item.requisition_status = "已报料"
            item.special_process = first_line.special_process
            item.cardboard_len = first_line.cardboard_len
            item.cardboard_width = first_line.cardboard_width
            item.requisition_spec = spec
            item.requisition_date = requisition_date
            item.requisition_remark = (first_line.remark or "").strip() or None
            for component in components:
                component_remark = item.requisition_remark
                if component["report_notes"] and component["report_notes"] != component_remark:
                    component_remark = "；".join(
                        part
                        for part in [component_remark, str(component["report_notes"]).strip()]
                        if part
                    )
                batch_item = RequisitionItem(
                    requisition_id=batch.id,
                    order_item_id=item.id,
                    inventory_deducted_qty=0,
                    requisition_qty=int(component["requisition_qty"]),
                    cardboard_len=component["cardboard_len"],
                    cardboard_width=component["cardboard_width"],
                    pieces_per_box=pieces_per_box,
                    required_piece_qty=required_piece_qty,
                    special_process=component["special_process"],
                    material_snapshot=item.snapshot_material,
                    product_code_snapshot=(item.snapshot_product_code or product.product_code),
                    product_name_snapshot=(
                        f"{item.snapshot_product_name}-{component['suffix']}"
                        if len(components) > 1
                        else item.snapshot_product_name
                    ),
                    specification_snapshot=item.snapshot_spec,
                    remark=component_remark,
                    status="有效",
                )
                db.add(batch_item)
            response_items.append(_item_response(item, db))
        _audit(
            db,
            user=user,
            action="CREATE_REQUISITION",
            entity_id=batch.id,
            details={
                "requisition_number": batch.requisition_number,
                "item_ids": list(lines_by_order_item),
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
    if payload.inventory_deducted_qty:
        raise HTTPException(
            status_code=400,
            detail="旧库存抵扣字段已停用，真实抵扣只能来自成品库存预占",
        )
    pieces_per_box = _pieces_per_box(item)
    production_required_qty = max(
        item.quantity - active_finished_reserved_qty(db, item.id), 0
    )
    required_piece_qty = _required_piece_qty(
        production_required_qty, pieces_per_box
    )
    item.inventory_deducted_qty = 0
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
            inventory_deducted_qty=0,
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
    return _item_response(item, db)


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
    return _item_response(item, db)


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
    return _item_response(item, db)


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
            Order.status.notin_(["cancelled", "dead", "closed", "archived"]),
            OrderItem.is_force_closed.is_(False),
        )
        .order_by(OrderItem.created_at.desc(), OrderItem.id.desc())
    ).all()

    reservation_map = active_finished_reservations_by_item_ids(
        db, [item.id for item, *_ in rows]
    )

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
        requirements = _current_requisition_requirements(
            db,
            item,
            cutting_mode=DEFAULT_CUTTING_MODE,
            finished_reserved_qty=reservation_map.get(item.id, 0),
        )
        pieces_per_box = int(requirements["pieces_per_box"])
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        if production_required_qty == 0:
            continue
        required_piece_qty = int(requirements["required_piece_qty"])
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
                item.flute_type,
                fallback_text=item.snapshot_material,
            ),
            "snapshot_supplier_name": item.snapshot_supplier_name,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "quantity": item.quantity,
            "finished_inventory_reserved_qty": finished_reserved_qty,
            "production_required_qty": production_required_qty,
            "pieces_per_box": pieces_per_box,
            "required_piece_qty": required_piece_qty,
            "requisition_qty": int(requirements["requisition_qty"]),
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
                flute_type,
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
            "dimension_warnings": _supplier_dimension_warnings(
                supplier_name,
                report_len,
                report_width,
                DEFAULT_CUTTING_MODE,
            ),
        })

    return {"suggestions": suggestions}


@router.post("/merge-groups", status_code=status.HTTP_201_CREATED)
def create_merge_group(
    payload: MergeGroupCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    rows = _validate_merge_member_rows(db, payload.member_item_ids)
    reservation_map = active_finished_reservations_by_item_ids(
        db, [item.id for item, *_ in rows]
    )
    requisition_date = date.today()
    try:
        group = Requisition(
            requisition_number=_next_number(db, requisition_date),
            requisition_date=requisition_date,
            supplier_name=(payload.supplier_name or "").strip() or None,
            status="merged_pending",
            created_by=user.id,
        )
        db.add(group)
        db.flush()
        for item, _order, _customer, product in rows:
            pieces_per_box = _pieces_per_box(item)
            production_required_qty = max(
                item.quantity - reservation_map.get(item.id, 0),
                0,
            )
            if production_required_qty == 0:
                raise HTTPException(
                    status_code=409,
                    detail="所选明细已由成品库存全额抵扣，不能创建待报料合并组",
                )
            required_piece_qty = _required_piece_qty(
                production_required_qty,
                pieces_per_box,
            )
            db.add(
                RequisitionItem(
                    requisition_id=group.id,
                    order_item_id=item.id,
                    inventory_deducted_qty=0,
                    requisition_qty=_purchase_qty(required_piece_qty, 0, payload.cutting_mode),
                    cardboard_len=payload.report_length_mm,
                    cardboard_width=payload.report_width_mm,
                    pieces_per_box=pieces_per_box,
                    required_piece_qty=required_piece_qty,
                    special_process=payload.cutting_mode,
                    material_snapshot=item.snapshot_material,
                    product_code_snapshot=item.snapshot_product_code or product.product_code,
                    product_name_snapshot=item.snapshot_product_name,
                    specification_snapshot=item.snapshot_spec,
                    remark=(payload.remark or "").strip() or None,
                    status="merged_pending",
                )
            )
        _audit(
            db,
            user=user,
            action="CREATE_REQUISITION_MERGE_GROUP",
            entity_id=group.id,
            details={"member_item_ids": payload.member_item_ids},
            description="创建待报料合并组",
        )
        db.commit()
        db.refresh(group)
        return _merge_group_dict(group, db)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/merge-groups/{group_id}")
def update_merge_group(
    group_id: int,
    payload: MergeGroupUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    group = db.get(Requisition, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="待报料合并组不存在")
    if group.status != "merged_pending":
        raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能修改")
    try:
        if payload.supplier_name is not None:
            group.supplier_name = payload.supplier_name.strip() or None
        updates = {
            key: value
            for key, value in {
                "cardboard_len": payload.report_length_mm,
                "cardboard_width": payload.report_width_mm,
                "special_process": payload.cutting_mode,
                "remark": (payload.remark.strip() if payload.remark is not None else None),
            }.items()
            if value is not None
        }
        for item in group.items:
            for key, value in updates.items():
                setattr(item, key, value)
            order_item = db.get(OrderItem, item.order_item_id)
            if order_item is None:
                raise HTTPException(status_code=404, detail="合并组订单明细不存在")
            requirements = _current_requisition_requirements(
                db,
                order_item,
                cutting_mode=item.special_process,
                pieces_per_box=item.pieces_per_box or _pieces_per_box(order_item),
            )
            item.pieces_per_box = int(requirements["pieces_per_box"])
            item.required_piece_qty = int(requirements["required_piece_qty"])
            item.requisition_qty = int(requirements["requisition_qty"])
        _audit(
            db,
            user=user,
            action="UPDATE_REQUISITION_MERGE_GROUP",
            entity_id=group.id,
            details={"group_id": group.id},
            description="修改待报料合并组",
        )
        db.commit()
        db.refresh(group)
        return _merge_group_dict(group, db)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/merge-groups/{group_id}/supplier-order", status_code=status.HTTP_201_CREATED)
def create_supplier_order_from_merge_group(
    group_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    group = db.get(Requisition, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="待报料合并组不存在")
    if group.status != "merged_pending":
        raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
    supplier_name = (group.supplier_name or "").strip()
    if not supplier_name:
        raise HTTPException(status_code=400, detail="请先为合并组选择供应商")
    rows = _merge_group_rows(db, group.id)
    if not rows:
        raise HTTPException(status_code=400, detail="合并组没有来源明细")
    current_rows: list[tuple] = []
    for req_item, order_item, order_row, customer, product in rows:
        if req_item.status != "merged_pending":
            raise HTTPException(status_code=409, detail="合并组状态异常，不能生成供应商报料单")
        if order_item.requisition_status != "未报料":
            raise HTTPException(status_code=409, detail="合并组中存在已报料明细，不能重复生成")
        requirements = _current_requisition_requirements(
            db,
            order_item,
            cutting_mode=req_item.special_process,
            pieces_per_box=req_item.pieces_per_box or _pieces_per_box(order_item),
        )
        if bool(requirements["fully_covered_by_finished_inventory"]):
            raise HTTPException(
                status_code=409,
                detail="合并组中存在已由成品库存全额抵扣的明细，请刷新后重试",
            )
        req_item.pieces_per_box = int(requirements["pieces_per_box"])
        req_item.required_piece_qty = int(requirements["required_piece_qty"])
        req_item.requisition_qty = int(requirements["requisition_qty"])
        current_rows.append(
            (
                req_item,
                order_item,
                order_row,
                customer,
                product,
                requirements,
            )
        )

    first_req_item, first_order_item, *_ = current_rows[0]
    material = db.get(Material, first_order_item.material_id) if first_order_item.material_id else None
    total_quantity = sum(
        int(requirements["production_required_qty"])
        for *_, requirements in current_rows
    )
    total_stock_deduction_qty = sum(
        int(requirements["finished_inventory_reserved_qty"])
        for *_, requirements in current_rows
    )
    total_required_piece_qty = sum(
        int(requirements["required_piece_qty"])
        for *_, requirements in current_rows
    )
    total_requisition_qty = sum(
        int(requirements["requisition_qty"])
        for *_, requirements in current_rows
    )
    order = SupplierRequisitionOrder(
        order_number=_supplier_order_number(db),
        supplier_name=supplier_name,
        material_id=first_order_item.material_id,
        layer_count=first_order_item.layer_count or (material.layer_count if material else None),
        flute_type=first_order_item.flute_type,
        report_length_mm=int(first_req_item.cardboard_len),
        report_width_mm=int(first_req_item.cardboard_width),
        crease_type=first_order_item.snapshot_crease_type,
        crease_left_mm=first_order_item.snapshot_crease_left_mm,
        crease_middle_mm=first_order_item.snapshot_crease_middle_mm,
        crease_right_mm=first_order_item.snapshot_crease_right_mm,
        cutting_mode=first_req_item.special_process,
        pieces_per_box=first_req_item.pieces_per_box,
        required_piece_qty=total_required_piece_qty,
        total_quantity=total_quantity,
        stock_deduction_qty=total_stock_deduction_qty,
        requisition_qty=total_requisition_qty,
        remark=first_req_item.remark,
        status="confirmed",
        created_by=user.id,
    )
    try:
        db.add(order)
        db.flush()
        requisition_date = date.today()
        for (
            req_item,
            order_item,
            _order,
            customer,
            _product,
            requirements,
        ) in current_rows:
            db.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=order.id,
                    order_item_id=order_item.id,
                    order_number=order_item.item_order_number,
                    product_code=req_item.product_code_snapshot,
                    product_name=req_item.product_name_snapshot,
                    quantity=int(requirements["production_required_qty"]),
                    stock_deduction_qty=int(
                        requirements["finished_inventory_reserved_qty"]
                    ),
                    requisition_qty=int(requirements["requisition_qty"]),
                    cutting_mode=str(requirements["cutting_mode"]),
                    pieces_per_box=int(requirements["pieces_per_box"]),
                    required_piece_qty=int(requirements["required_piece_qty"]),
                    customer_name=customer.name,
                    delivery_date=_order.delivery_date,
                )
            )
            order_item.inventory_deducted_qty = 0
            order_item.requisition_status = "已报料"
            order_item.requisition_qty = int(requirements["requisition_qty"])
            order_item.special_process = str(requirements["cutting_mode"])
            order_item.cardboard_len = req_item.cardboard_len
            order_item.cardboard_width = req_item.cardboard_width
            order_item.requisition_spec = (
                f"{_plain(req_item.cardboard_len)}×{_plain(req_item.cardboard_width)}"
            )
            order_item.requisition_date = requisition_date
            order_item.requisition_remark = req_item.remark
            req_item.status = "supplier_requisition_created"
        group.status = "supplier_requisition_created"
        _audit(
            db,
            user=user,
            action="CREATE_SUPPLIER_ORDER_FROM_MERGE_GROUP",
            entity_id=group.id,
            details={
                "merge_group_id": group.id,
                "supplier_order_id": order.id,
                "supplier_order_number": order.order_number,
            },
            description="待报料合并组生成供应商报料单",
        )
        db.commit()
        db.refresh(order)
        return {
            "supplier_order_id": order.id,
            "supplier_order_number": order.order_number,
            "status": "created",
            "supplier_order": _supplier_order_dict(order, db),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/batches/{batch_id}/print")
def print_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    batch = db.get(Requisition, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="报料单不存在")
    if batch.status in {"merged_pending", "supplier_requisition_created"}:
        raise HTTPException(status_code=409, detail="待报料合并组不是正式报料单，不能打印")
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
        material_flute_type = None
        component = (
            "base"
            if row.product_name_snapshot and row.product_name_snapshot.endswith("-底")
            else "cover"
        )
        if order_item:
            crease_type, crease_left, crease_middle, crease_right = _component_crease(
                order_item, component
            )
        else:
            crease_type, crease_left, crease_middle, crease_right = (None, None, None, None)
        if crease_type == "压线" and crease_middle is not None:
            crease_display = (
                f"{crease_left or 0}+"
                f"{crease_middle}+"
                f"{crease_right or 0}"
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
        component_report_notes = None
        if order_item:
            component_report_notes = (
                order_item.snapshot_base_report_notes
                if component == "base"
                else order_item.snapshot_report_notes
            )
        if component_report_notes:
            remarks.append(component_report_notes)
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


def _first_int_value(*values) -> int | None:
    for value in values:
        if value is None:
            continue
        parsed = _int_value(value, default=0)
        if parsed:
            return parsed
    return None


def _source_items_from_supplier_order(order: SupplierRequisitionOrder) -> list[dict]:
    return [
        {
            "id": item.id,
            "order_item_id": item.order_item_id,
            "order_number": item.order_number,
            "product_code": item.product_code,
            "product_name": item.product_name,
            "quantity": item.quantity,
            "source_quantity": item.quantity,
            "stock_deduction_qty": item.stock_deduction_qty,
            "inventory_deducted_qty": item.stock_deduction_qty,
            "requisition_qty": item.requisition_qty,
            "cutting_mode": item.cutting_mode or order.cutting_mode or DEFAULT_CUTTING_MODE,
            "pieces_per_box": item.pieces_per_box,
            "required_piece_qty": item.required_piece_qty,
            "customer_name": item.customer_name,
            "delivery_date": item.delivery_date,
        }
        for item in order.items
    ]


def _supplier_order_purchase_lines(
    order: SupplierRequisitionOrder,
    db: Session,
) -> list[dict]:
    line_map: dict[str, dict] = {}
    for item in order.items:
        order_item = db.get(OrderItem, item.order_item_id) if item.order_item_id else None
        material_id = order_item.material_id if order_item and order_item.material_id else order.material_id
        material = db.get(Material, material_id) if material_id else None
        layer_count = (
            order_item.layer_count
            if order_item is not None and order_item.layer_count
            else order.layer_count or (material.layer_count if material else None)
        )
        flute_type = _clean_supplier_flute(
            order_item.flute_type if order_item is not None and order_item.flute_type else order.flute_type
        )
        material_code = material.code if material else (order_item.snapshot_material if order_item else None)
        report_length = _first_int_value(
            order_item.cardboard_len if order_item is not None else None,
            order_item.snapshot_report_length_mm if order_item is not None else None,
            order.report_length_mm,
        )
        report_width = _first_int_value(
            order_item.cardboard_width if order_item is not None else None,
            order_item.snapshot_report_width_mm if order_item is not None else None,
            order.report_width_mm,
        )
        crease_type = (
            order_item.snapshot_crease_type
            if order_item is not None and order_item.snapshot_crease_type
            else order.crease_type
        )
        crease_left = (
            order_item.snapshot_crease_left_mm
            if order_item is not None and order_item.snapshot_crease_left_mm is not None
            else order.crease_left_mm
        )
        crease_middle = (
            order_item.snapshot_crease_middle_mm
            if order_item is not None and order_item.snapshot_crease_middle_mm is not None
            else order.crease_middle_mm
        )
        crease_right = (
            order_item.snapshot_crease_right_mm
            if order_item is not None and order_item.snapshot_crease_right_mm is not None
            else order.crease_right_mm
        )
        cutting_mode = item.cutting_mode or (
            order_item.special_process if order_item is not None else None
        ) or order.cutting_mode or DEFAULT_CUTTING_MODE
        remark = (
            order_item.requisition_remark if order_item is not None and order_item.requisition_remark else order.remark
        ) or ""
        material_display = _format_supplier_material(
            material_code,
            layer_count,
            flute_type,
            fallback_text=order_item.snapshot_material if order_item is not None else None,
        )
        spec = {
            "material_id": material_id,
            "material_code": _clean_supplier_material_code(material_code, layer_count),
            "material_display": material_display,
            "layer_count": layer_count,
            "flute_type": flute_type,
            "report_length_mm": report_length,
            "report_width_mm": report_width,
            "crease_type": crease_type,
            "crease_left_mm": crease_left,
            "crease_middle_mm": crease_middle,
            "crease_right_mm": crease_right,
            "cutting_mode": cutting_mode,
            "remark": remark,
        }
        line_key = _purchase_line_key(order.supplier_name, spec)
        line = line_map.get(line_key)
        if line is None:
            crease_display = (
                f"{crease_left}+{crease_middle}+{crease_right}"
                if crease_type == "压线" and crease_middle
                else crease_type or "-"
            )
            line = {
                "line_key": line_key,
                **spec,
                "crease_display": crease_display,
                "dimension_warnings": _supplier_dimension_warnings(
                    order.supplier_name,
                    report_length,
                    report_width,
                    cutting_mode,
                ),
                "quantity": 0,
                "production_required_qty": 0,
                "required_piece_qty": 0,
                "stock_deduction_qty": 0,
                "inventory_deducted_qty": 0,
                "requisition_qty": 0,
                "source_items": [],
            }
            line_map[line_key] = line
        line["quantity"] += int(item.quantity or 0)
        line["production_required_qty"] = line["quantity"]
        line["required_piece_qty"] += int(item.required_piece_qty or 0)
        line["stock_deduction_qty"] += int(item.stock_deduction_qty or 0)
        line["inventory_deducted_qty"] = line["stock_deduction_qty"]
        line["requisition_qty"] += int(item.requisition_qty or 0)
        line["source_items"].append(
            {
                "id": item.id,
                "order_item_id": item.order_item_id,
                "order_number": item.order_number,
                "product_code": item.product_code,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "source_quantity": item.quantity,
                "stock_deduction_qty": item.stock_deduction_qty,
                "inventory_deducted_qty": item.stock_deduction_qty,
                "requisition_qty": item.requisition_qty,
                "cutting_mode": item.cutting_mode or order.cutting_mode or DEFAULT_CUTTING_MODE,
                "pieces_per_box": item.pieces_per_box,
                "required_piece_qty": item.required_piece_qty,
                "customer_name": item.customer_name,
                "delivery_date": item.delivery_date,
            }
        )
    return list(line_map.values())


def _supplier_order_dict(order: SupplierRequisitionOrder, db: Session) -> dict:
    material = db.get(Material, order.material_id) if order.material_id else None
    material_code = material.code if material else None
    material_layer_count = order.layer_count or (material.layer_count if material else None)
    material_flute_type = order.flute_type
    crease_display = (
        f"{order.crease_left_mm}+{order.crease_middle_mm}+{order.crease_right_mm}"
        if order.crease_type == "压线" and order.crease_middle_mm
        else order.crease_type or "-"
    )
    source_items = _source_items_from_supplier_order(order)
    purchase_lines = _supplier_order_purchase_lines(order, db)
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
        "dimension_warnings": _supplier_dimension_warnings(
            order.supplier_name,
            order.report_length_mm,
            order.report_width_mm,
            order.cutting_mode or DEFAULT_CUTTING_MODE,
        ),
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
        "lines": purchase_lines,
        "items": purchase_lines,
        "source_items": source_items,
    }


@router.post("/supplier-orders/preview-from-pending-selection")
def preview_supplier_orders_from_pending_selection(
    payload: PendingSupplierOrderCreatePayload,
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    return _pending_selection_preview_groups(db, payload)


@router.post("/supplier-orders/from-pending-selection", status_code=status.HTTP_201_CREATED)
def create_supplier_orders_from_pending_selection(
    payload: PendingSupplierOrderFinalizePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        grouped, touched_groups = _draft_group_entries_by_purchase_lines(db, payload)
        created_orders: list[SupplierRequisitionOrder] = []
        for supplier_name, entries in grouped.items():
            created_orders.append(
                _create_supplier_order_for_pending_entries(
                    db,
                    supplier_name=supplier_name,
                    entries=entries,
                    user=user,
                )
            )
        for group in touched_groups:
            group.status = "supplier_requisition_created"
        db.flush()
        _audit(
            db,
            user=user,
            action="CREATE_SUPPLIER_ORDERS_FROM_PENDING_SELECTION",
            entity_id=created_orders[0].id if created_orders else None,
            details={
                "supplier_order_ids": [order.id for order in created_orders],
                "supplier_names": [order.supplier_name for order in created_orders],
                "supplier_group_count": len(payload.supplier_groups),
            },
            description="待报料列表按供应商合并生成供应商报料单",
        )
        db.commit()
        for order in created_orders:
            db.refresh(order)
        return {
            "created_orders": [
                {
                    "supplier_name": order.supplier_name,
                    "supplier_order_id": order.id,
                    "supplier_order_number": order.order_number,
                    "pdf_url": f"/api/requisition/supplier-orders/{order.id}/pdf",
                    "item_count": len(order.items),
                }
                for order in created_orders
            ]
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/supplier-orders", status_code=status.HTTP_201_CREATED)
def create_supplier_order(
    payload: SupplierOrderCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if not payload.members:
        raise HTTPException(status_code=400, detail="至少需要一条明细")

    validated_members: list[dict] = []
    for member in payload.members:
        if member.stock_deduction_qty:
            raise HTTPException(
                status_code=400,
                detail="旧库存抵扣字段已停用，真实抵扣只能来自成品库存预占",
            )
        order_item = db.get(OrderItem, member.item_id) if member.item_id else None
        if member.item_id is not None and order_item is None:
            raise HTTPException(status_code=404, detail="订单明细不存在")
        if order_item is None:
            pieces_per_box = max(int(member.pieces_per_box or 1), 1)
            production_required_qty = max(int(member.quantity or 0), 0)
            required_piece_qty = max(
                int(
                    member.required_piece_qty
                    if member.required_piece_qty is not None
                    else production_required_qty * pieces_per_box
                ),
                0,
            )
            cutting_mode = member.cutting_mode or payload.cutting_mode
            validated_members.append(
                {
                    "payload": member,
                    "order_item": None,
                    "production_required_qty": production_required_qty,
                    "finished_reserved_qty": 0,
                    "pieces_per_box": pieces_per_box,
                    "required_piece_qty": required_piece_qty,
                    "cutting_mode": cutting_mode,
                    "requisition_qty": _purchase_qty(
                        required_piece_qty, 0, cutting_mode
                    ),
                }
            )
            continue
        if order_item.requisition_status != "未报料":
            raise HTTPException(status_code=409, detail="订单明细已经报料")
        finished_reserved_qty = active_finished_reserved_qty(db, order_item.id)
        production_required_qty = max(
            order_item.quantity - finished_reserved_qty, 0
        )
        if production_required_qty == 0:
            raise HTTPException(
                status_code=409,
                detail="该订单明细已由成品库存全额抵扣，无需生成供应商报料单",
            )
        pieces_per_box = _pieces_per_box(order_item)
        required_piece_qty = _required_piece_qty(
            production_required_qty, pieces_per_box
        )
        cutting_mode = member.cutting_mode or payload.cutting_mode
        requisition_qty = _purchase_qty(
            required_piece_qty, 0, cutting_mode
        )
        validated_members.append(
            {
                "payload": member,
                "order_item": order_item,
                "production_required_qty": production_required_qty,
                "finished_reserved_qty": finished_reserved_qty,
                "pieces_per_box": pieces_per_box,
                "required_piece_qty": required_piece_qty,
                "cutting_mode": cutting_mode,
                "requisition_qty": requisition_qty,
            }
        )

    order_number = _supplier_order_number(db)
    total_qty = sum(row["production_required_qty"] for row in validated_members)
    total_deduct = sum(row["finished_reserved_qty"] for row in validated_members)
    total_req = sum(row["requisition_qty"] for row in validated_members)
    total_required_piece_qty = sum(
        row["required_piece_qty"] for row in validated_members
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
        cutting_mode=payload.cutting_mode,
        pieces_per_box=payload.pieces_per_box,
        required_piece_qty=total_required_piece_qty,
        total_quantity=total_qty,
        stock_deduction_qty=total_deduct,
        requisition_qty=total_req,
        remark=payload.remark,
        status="confirmed",
        created_by=user.id,
    )
    db.add(order)
    db.flush()

    for validated in validated_members:
        m = validated["payload"]
        req_qty = validated["requisition_qty"]
        oi = validated["order_item"]
        db.add(SupplierRequisitionOrderItem(
            supplier_order_id=order.id,
            order_item_id=m.item_id,
            order_number=m.order_number,
            product_code=m.product_code,
            product_name=m.product_name,
            quantity=validated["production_required_qty"],
            stock_deduction_qty=validated["finished_reserved_qty"],
            requisition_qty=req_qty,
            cutting_mode=validated["cutting_mode"],
            pieces_per_box=validated["pieces_per_box"],
            required_piece_qty=validated["required_piece_qty"],
            customer_name=m.customer_name,
            delivery_date=date.fromisoformat(m.delivery_date) if m.delivery_date else None,
        ))
        if oi:
            oi.requisition_status = "已报料"
            oi.inventory_deducted_qty = 0
            oi.requisition_qty = req_qty
            oi.special_process = validated["cutting_mode"]

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


@router.get("/reported-documents")
def list_reported_documents(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    documents: list[dict] = []
    supplier_orders = db.scalars(
        select(SupplierRequisitionOrder).order_by(
            SupplierRequisitionOrder.created_at.desc(),
            SupplierRequisitionOrder.id.desc(),
        )
    ).all()
    for order in supplier_orders:
        order_numbers = _unique_text([item.order_number for item in order.items])
        product_codes = _unique_text([item.product_code for item in order.items])
        customer_names = _unique_text([item.customer_name for item in order.items])
        documents.append(
            {
                "source_type": "supplier_order",
                "id": order.id,
                "document_number": order.order_number,
                "supplier_name": order.supplier_name,
                "status": order.status,
                "incoming_status": "已作废" if order.status == "voided" else "待入库",
                "created_at": order.created_at,
                "item_count": len(order.items),
                "order_numbers": order_numbers,
                "product_codes": product_codes,
                "customer_names": customer_names,
                "requisition_qty": order.requisition_qty,
                "pdf_url": f"/api/requisition/supplier-orders/{order.id}/pdf",
            }
        )

    legacy_batches = db.scalars(
        select(Requisition)
        .where(Requisition.status.notin_(["merged_pending", "supplier_requisition_created"]))
        .order_by(Requisition.created_at.desc(), Requisition.id.desc())
    ).all()
    for batch in legacy_batches:
        order_numbers: list[str | None] = []
        product_codes: list[str | None] = []
        customer_names: list[str | None] = []
        total_requisition_qty = 0
        for item in batch.items:
            total_requisition_qty += int(item.requisition_qty or 0)
            product_codes.append(item.product_code_snapshot)
            order_item = db.get(OrderItem, item.order_item_id)
            if order_item is None:
                continue
            order = db.get(Order, order_item.order_id)
            if order is not None:
                order_numbers.append(display_order_number(order, registry))
                customer = db.get(Customer, order.customer_id)
                if customer is not None:
                    customer_names.append(customer.name)
        documents.append(
            {
                "source_type": "legacy_material_requisition",
                "id": batch.id,
                "document_number": batch.requisition_number,
                "supplier_name": batch.supplier_name,
                "status": batch.status,
                "incoming_status": "待入库",
                "created_at": batch.created_at,
                "item_count": len(batch.items),
                "order_numbers": _unique_text(order_numbers),
                "product_codes": _unique_text(product_codes),
                "customer_names": _unique_text(customer_names),
                "requisition_qty": total_requisition_qty,
                "pdf_url": f"/requisition-print.html?id={batch.id}",
            }
        )
    documents.sort(
        key=lambda row: (
            row["created_at"] or datetime.min,
            row["id"],
        ),
        reverse=True,
    )
    return {"total": len(documents), "items": documents}


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
