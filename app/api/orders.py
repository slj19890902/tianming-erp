from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

import jwt
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.config import load_settings
from app.core.time_contract import (
    beijing_naive_to_api,
    beijing_now_naive,
    beijing_today,
    utc_naive_to_api,
)
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
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion
from app.models.product_bom import (
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.tianhua_pre_delivery import (
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
    TianhuaPreDeliveryImportItem,
)
from app.models.user import User
from app.models.warehouse_inventory import DeliveryInventoryAllocation, InventoryLot
from app.services.history_orders import (
    build_display_registry,
    filter_order_ids_for_display_search,
    sanitize_user_text,
    serialize_order_number_fields,
)
from app.services.flute_mapping import (
    normalize_flute_type,
    seven_layer_code_error,
    validate_flute_for_write,
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
    _company_name_key,
    _full_company_name_key,
    calculate_draft_cost,
    file_sha256,
    match_import_draft,
)
from app.services.pdf_customer_templates import load_active_pdf_template_rules
from app.services.pdf_parse_pipeline import parse_pdf_bytes
from app.services.pdf_preview_redaction import redact_pdf_preview_for_user
from app.services.product_import import (
    NewProductError,
    NewProductInput,
    parse_dimensions,
    resolve_or_create_product,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    create_or_refresh_production_task,
    has_production_completion_facts,
    lock_order_rows_for_production_transition,
    refresh_order_production_status,
    refresh_production_task,
)
from app.services.composite_bom import (
    CompositeBOMError,
    create_order_item_bom_snapshots,
    get_order_item_bom_components_by_item_ids,
    get_order_item_bom_preview,
    is_composite_product,
    raise_http as raise_composite_bom_http,
)
from app.services.composite_bom_workflow import (
    CompositeBomWorkflowError,
    append_order_quantity_adjustments,
    is_composite_order_item,
)
from app.services.report_crease import crease_width_error, product_crease_width_error
from app.services.product_drawings import (
    DrawingValidationError,
    remove_drawing_files,
    save_product_drawing_files,
)
from app.services.secure_uploads import (
    DRAWING_POLICY,
    PDF_POLICY,
    UploadTokenError,
    UploadValidationError,
    consume_temporary_token,
    create_temporary_token,
    read_validated_upload,
    resolve_stored_reference,
    stored_file_metadata,
    temporary_token_file,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    active_finished_reservations_by_item_ids,
    has_unconsumed_inventory_reservations,
    normalize_material_code,
    release_active_finished_reservations_for_items,
    reserve_finished_inventory,
)
from app.services.semi_finished_inventory import (
    GENERAL_SEMI_FINISHED_STOCK,
    SIGNATURE_OVERRIDE_WARNING,
    SemiFinishedLotVersion,
    SemiFinishedSignature,
    active_semi_coverage_by_order_item,
    browse_semi_finished_inventory,
    ensure_semi_finished_lot_eligibility,
    release_active_semi_reservations_for_items,
    reserve_semi_finished_inventory,
    save_order_item_semi_requirement,
    semi_finished_inventory_candidates,
)


router = APIRouter()
can_create = PermissionChecker("orders.create")
can_read = PermissionChecker("orders.view")
can_edit = PermissionChecker("orders.edit")
can_status = PermissionChecker("orders.status")
can_delete = PermissionChecker("orders.delete")
can_rollback = PermissionChecker("orders.rollback")
can_view_cost = PermissionChecker("cost.view")
_PRODUCT_DRAWING_SAVE_OPTIONS = frozenset({"save_to_product", "overwrite_product"})
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

_PRODUCTION_FACT_CONFLICT = "订单明细已有生产完工或转库存事实，不能执行该操作。"


_PRODUCT_ID_SENTINELS = {"", "new_product", "null", "undefined", "none", "nan"}


def _safe_drawing_suffix(reference: str | None) -> str:
    suffix = Path(reference or "").suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".pdf"} else ".bin"


def _order_drawing_url(item_id: int, reference: str | None) -> str | None:
    if not reference:
        return None
    return f"/api/orders/items/{item_id}/drawing/content/file{_safe_drawing_suffix(reference)}"


def _product_drawing_url(drawing) -> str | None:
    if drawing is None:
        return None
    return (
        f"/api/master/products/drawings/{drawing.id}/content/"
        f"original{_safe_drawing_suffix(drawing.image_path)}"
    )


def _require_product_drawing_edit(user: User) -> None:
    if not has_permission(user, "products.edit"):
        raise HTTPException(status_code=403, detail="权限不足")

# Terminal / archived statuses that should NOT appear in the day-to-day
# "business" (日常订单) view. Active orders — including freshly saved PDF
# imports that are still pending_production / awaiting requisition — stay in
# business. Orders whose item quantities are already fully delivered are also
# excluded below even if an old status snapshot has not been refreshed yet.
# Do not hide solely on completed/delivered snapshots: quantity is the source
# of truth for this view, and inconsistent legacy rows must stay discoverable.
_BUSINESS_EXCLUDED_STATUSES = ("dead", "cancelled", "closed", "archived")
_INACTIVE_SUPPLIER_REQUISITION_ORDER_STATUSES = [
    "voided", "cancelled", "canceled", "withdrawn", "invalid",
    "已作废", "已取消", "已撤回",
]
_ROLLBACK_EDITABLE_SUPPLIER_REQUISITION_ORDER_STATUSES = {
    "confirmed", "draft", "pending",
}


class FinishedReservationPlanEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lot_id: int
    expected_version: int = Field(gt=0)
    requested_qty: int = Field(
        validation_alias=AliasChoices(
            "requested_qty", "requested_quantity", "quantity"
        ),
        gt=0,
    )
    recommendation_source: str = "dedicated"
    match_rule_id: int | None = None
    override: bool = False
    confirmed: bool = False


class SemiReservationPlanEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lot_id: int
    expected_version: int = Field(gt=0)
    requested_qty: int = Field(
        validation_alias=AliasChoices(
            "requested_qty", "requested_quantity", "quantity"
        ),
        gt=0,
    )
    component_type: Literal["whole", "cover", "base"] = "whole"
    recommendation_source: Literal[
        "learned", "signature", "general_signature", "manual"
    ]
    match_rule_id: int | None = None
    override: bool = False
    confirmed: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)


class OrderItemReservationPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finished: list[FinishedReservationPlanEntry] = Field(default_factory=list)
    semi: list[SemiReservationPlanEntry] = Field(default_factory=list)


class OrderItemCreate(BaseModel):
    client_line_id: str | None = Field(default=None, max_length=100)
    reservation_plan: OrderItemReservationPlan | None = None
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
    temp_drawing_token: str | None = Field(default=None, min_length=32, max_length=32)
    drawing_save_option: Literal[
        "order_only", "save_to_product", "overwrite_product"
    ] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_client_filesystem_path(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("temp_drawing_file") not in (None, ""):
            raise ValueError(
                "temp_drawing_file 已停用；请先上传图纸并提交一次性 temp_drawing_token"
            )
        return value

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


class PdfImportConfirmation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    preview_safety_token: str = Field(min_length=1, max_length=4000)
    confirmed: bool = False


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
    sync_product: bool = False
    product_expected_version: int | None = Field(default=None, ge=1)
    product_change_reason: str | None = Field(default=None, max_length=500)
    product_confirmation_token: str | None = Field(default=None, max_length=2000)
    box_style: str | None = None
    length_mm: int | None = Field(default=None, gt=0)
    width_mm: int | None = Field(default=None, gt=0)
    height_mm: int | None = Field(default=None, gt=0)
    production_process: str | None = None
    print_content: str | None = None
    product_remark: str | None = None
    quantity_adjustment_reason: str | None = Field(default=None, max_length=500)
    quantity_adjustment_idempotency_key: str | None = Field(
        default=None,
        max_length=120,
    )


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
    import_draft: bool = False
    pdf_import_confirmation: PdfImportConfirmation | None = None

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


PDF_PREVIEW_SAFETY_TOKEN_TTL_MINUTES = 5
PDF_PREVIEW_SAFETY_TOKEN_TYPE = "pdf_order_preview_safety"


def _pdf_preview_actor(user: User) -> str:
    if user.id is not None:
        return f"id:{user.id}"
    return f"username:{user.username}"


def _pdf_preview_safety_states(draft: dict) -> dict:
    route = draft.get("customer_route")
    route_status = route.get("status") if isinstance(route, dict) else None
    integrity = draft.get("integrity_check")
    integrity_status = (
        integrity.get("integrity_status") if isinstance(integrity, dict) else None
    )
    matched_customer_id = draft.get("matched_customer_id")
    return {
        "recognition_status": str(draft.get("recognition_status") or "failed"),
        "customer_route_status": str(route_status or "unmatched"),
        "customer_match_status": str(
            draft.get("customer_match_status") or "unmatched"
        ),
        "integrity_status": str(integrity_status or "missing"),
        "matched_customer_id": int(matched_customer_id or 0),
    }


def _encode_pdf_preview_safety_token(
    draft: dict,
    user: User,
    *,
    state_overrides: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    states = {**_pdf_preview_safety_states(draft), **(state_overrides or {})}
    claims = {
        "sub": _pdf_preview_actor(user),
        "type": PDF_PREVIEW_SAFETY_TOKEN_TYPE,
        "source_name": str(draft.get("source_name") or ""),
        "source_hash": str(draft.get("file_hash") or ""),
        **states,
        "iat": now,
        "exp": now + timedelta(minutes=PDF_PREVIEW_SAFETY_TOKEN_TTL_MINUTES),
    }
    return jwt.encode(claims, load_settings().secret_key, algorithm="HS256")


def _attach_pdf_preview_safety_token(
    draft: dict,
    user: User,
    *,
    state_overrides: dict | None = None,
) -> dict:
    result = dict(draft)
    result["preview_safety_token"] = _encode_pdf_preview_safety_token(
        result,
        user,
        state_overrides=state_overrides,
    )
    return result


def _finalize_pdf_preview_for_user(
    draft: dict,
    user: User,
    *,
    state_overrides: dict | None = None,
) -> dict:
    """Attach the trusted preview token, then enforce the cost-view boundary."""

    tokenized = _attach_pdf_preview_safety_token(
        draft,
        user,
        state_overrides=state_overrides,
    )
    return redact_pdf_preview_for_user(
        tokenized,
        can_view_cost=has_permission(user, "cost.view"),
    )


def _pdf_preview_token_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "PDF_PREVIEW_TOKEN_STALE", "message": message},
    )


def _decode_pdf_preview_safety_token(token: str, user: User) -> dict:
    try:
        claims = jwt.decode(
            token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={
                "require": [
                    "sub",
                    "type",
                    "source_name",
                    "source_hash",
                    "recognition_status",
                    "customer_route_status",
                    "customer_match_status",
                    "integrity_status",
                    "matched_customer_id",
                    "iat",
                    "exp",
                ]
            },
        )
    except jwt.ExpiredSignatureError as error:
        raise _pdf_preview_token_error("PDF 预览确认已过期，请重新预览") from error
    except jwt.PyJWTError as error:
        raise _pdf_preview_token_error("PDF 预览确认无效，请重新预览") from error
    if (
        claims.get("type") != PDF_PREVIEW_SAFETY_TOKEN_TYPE
        or claims.get("sub") != _pdf_preview_actor(user)
    ):
        raise _pdf_preview_token_error("PDF 预览确认与当前操作员不匹配")
    try:
        claims["matched_customer_id"] = int(claims["matched_customer_id"])
    except (TypeError, ValueError) as error:
        raise _pdf_preview_token_error("PDF 预览确认内容无效，请重新预览") from error
    return claims


def _validate_pdf_import_safety(
    payload: OrderCreate,
    user: User,
) -> tuple[list[str], dict]:
    context = payload.pdf_import_confirmation
    if context is None:
        if payload.import_draft:
            raise HTTPException(
                status_code=400,
                detail="PDF 草稿缺少服务端保存确认上下文，不能直接保存。请返回预览页重新确认。",
            )
        return [], {}

    if not context.confirmed:
        raise HTTPException(
            status_code=409,
            detail="PDF 草稿尚未执行明确确认，后端已拒绝直接保存。",
        )
    if payload.customer_id is None:
        raise HTTPException(status_code=400, detail="PDF 草稿必须明确选择客户")
    if not payload.items:
        raise HTTPException(status_code=400, detail="PDF 草稿至少需要一条明细")
    for index, item in enumerate(payload.items, start=1):
        if item.is_new_product or item.product_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条 PDF 明细必须明确选择唯一常用箱产品后才能直接保存",
            )

    claims = _decode_pdf_preview_safety_token(context.preview_safety_token, user)
    if claims["integrity_status"] != "passed":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PDF_PREVIEW_INTEGRITY_FAILED",
                "message": "PDF 完整性校验未通过或缺失，不能保存；请重新预览并补齐原单明细。",
            },
        )
    if (
        claims["customer_match_status"] != "matched"
        or claims["matched_customer_id"] != payload.customer_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PDF_PREVIEW_CUSTOMER_STALE",
                "message": "PDF 客户选择未经过最新重匹配确认，请重新选择客户。",
            },
        )

    reasons: list[str] = []
    if claims["recognition_status"] != "recognized":
        reasons.append(f"recognition_status={claims['recognition_status']}")
    if claims["customer_route_status"] != "locked":
        reasons.append(f"customer_route={claims['customer_route_status']}")
    return reasons, claims


def _validated_order_layer_flute(
    layer_count: int | None,
    flute_type: str | None,
    *,
    detail_prefix: str = "",
) -> str | None:
    """Validate the effective business flute before writing order snapshots."""
    normalized_flute = normalize_flute_type(flute_type)
    error = validate_flute_for_write(normalized_flute, layer_count)
    if error:
        raise HTTPException(status_code=400, detail=f"{detail_prefix}{error}")
    return normalized_flute


def _preflight_semi_signature(
    *,
    customer_id: int,
    product: Product,
    item_payload: OrderItemCreate,
    component_type: str,
    stock_yield_per_sheet: int,
) -> SemiFinishedSignature:
    is_base = component_type == "base"
    board_length_mm = (
        product.base_report_length_mm if is_base else product.report_length_mm
    )
    board_width_mm = (
        product.base_report_width_mm if is_base else product.report_width_mm
    )
    material_code = (item_payload.material or "").strip() or (
        product.material.code
        if product.material is not None
        else (product.legacy_material_text or "")
    )
    flute_type = (
        (item_payload.flute_type or "").strip().upper()
        or (product.flute_type or "").strip().upper()
    )
    if not (board_length_mm and board_width_mm and material_code and flute_type):
        raise WarehouseInventoryError(
            "订单明细缺少半成品长宽、材质或楞型，不能预校验库存抵扣", 409
        )
    pieces_per_box = (
        1
        if component_type in {"cover", "base"}
        else max(
            int(
                product.pieces_per_box
                or (2 if (product.splice_mode or "").lower() == "double" else 1)
            ),
            1,
        )
    )
    return SemiFinishedSignature(
        customer_id=customer_id,
        board_length_mm=int(board_length_mm),
        board_width_mm=int(board_width_mm),
        normalized_material_code=normalize_material_code(material_code),
        flute_type=flute_type,
        component_type=component_type,
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
    )


def _preflight_reservation_plans(
    db: Session,
    *,
    customer_id: int,
    payload_items: list[OrderItemCreate],
    resolved_products: dict[int, Product],
) -> dict[int, dict[str, int]]:
    states: dict[int, dict[str, int]] = {}
    client_line_ids: set[str] = set()
    for index, item_payload in enumerate(payload_items, start=1):
        if item_payload.client_line_id:
            client_line_id = item_payload.client_line_id.strip()
            if client_line_id in client_line_ids:
                raise WarehouseInventoryError(
                    f"第{index}条明细 client_line_id 重复", 409
                )
            client_line_ids.add(client_line_id)
        plan = item_payload.reservation_plan
        if plan is None:
            continue
        product = resolved_products[index]
        if is_composite_product(product):
            if plan.finished or plan.semi:
                raise WarehouseInventoryError(
                    f"第{index}条组合品明细不能建立父项库存抵扣计划",
                    409,
                )
            continue
        for entry in [*plan.finished, *plan.semi]:
            if not entry.confirmed:
                raise WarehouseInventoryError(
                    f"第{index}条明细库存抵扣计划尚未人工确认", 409
                )
            lot = db.get(InventoryLot, entry.lot_id)
            if lot is None:
                raise WarehouseInventoryError(
                    f"第{index}条明细所选库存批次不存在", 404
                )
            state = states.get(lot.id)
            if state is None:
                if lot.version != entry.expected_version:
                    raise WarehouseInventoryError(
                        "库存已被其他人修改，请刷新订单草稿候选后重试", 409
                    )
                states[lot.id] = {
                    "external_version": entry.expected_version,
                    "current_version": entry.expected_version,
                }
            elif state["external_version"] != entry.expected_version:
                raise WarehouseInventoryError(
                    "同一库存批次的草稿版本不一致，请刷新后重试", 409
                )
            if isinstance(entry, FinishedReservationPlanEntry):
                detail = lot.finished_detail
                if lot.inventory_type != "finished" or detail is None:
                    raise WarehouseInventoryError("所选批次不是成品库存", 409)
                if detail.is_general:
                    raise WarehouseInventoryError(
                        "新建订单不能使用通用成品库存，请选择同客户同存货编码的专用库存",
                        409,
                    )
                if detail.owner_customer_id != customer_id:
                    raise WarehouseInventoryError(
                        "其他客户专用成品库存不能用于当前订单", 409
                    )
                if (
                    detail.product_id != product.id
                    or detail.inventory_code_snapshot != product.product_code
                ):
                    raise WarehouseInventoryError(
                        "成品库存与订单存货编码不一致", 409
                    )
            else:
                detail = lot.semi_finished_detail
                if lot.inventory_type != "semi_finished" or detail is None:
                    raise WarehouseInventoryError("所选批次不是半成品库存", 409)
                expected = _preflight_semi_signature(
                    customer_id=customer_id,
                    product=product,
                    item_payload=item_payload,
                    component_type=entry.component_type,
                    stock_yield_per_sheet=detail.stock_yield_per_sheet,
                )
                scope = ensure_semi_finished_lot_eligibility(
                    db,
                    lot=lot,
                    product_id=product.id,
                    customer_id=customer_id,
                    expected=expected,
                )
                if scope == "general":
                    if entry.recommendation_source != "general_signature":
                        raise WarehouseInventoryError(
                            "通用半成品库存推荐来源已变化，请刷新", 409
                        )
                    if (
                        GENERAL_SEMI_FINISHED_STOCK
                        not in entry.warning_acknowledged_codes
                    ):
                        raise WarehouseInventoryError(
                            "通用半成品库存抵扣必须确认通用库存警告", 409
                        )
                    continue
                if entry.recommendation_source == "general_signature":
                    raise WarehouseInventoryError(
                        "专用半成品库存不能伪造为通用库存推荐", 409
                    )
                if entry.recommendation_source == "learned" and entry.match_rule_id is None:
                    raise WarehouseInventoryError("learned 推荐缺少学习规则标识", 409)
                if entry.recommendation_source == "manual":
                    if not entry.override:
                        raise WarehouseInventoryError(
                            "人工选择半成品库存必须明确 override", 409
                        )
                    if (
                        SIGNATURE_OVERRIDE_WARNING
                        not in entry.warning_acknowledged_codes
                    ):
                        raise WarehouseInventoryError(
                            "人工 override 必须确认半成品签名差异警告", 409
                        )
    return states


def _plan_current_version(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    states: dict[int, dict[str, int]],
) -> tuple[InventoryLot, int]:
    state = states.get(lot_id)
    lot = db.get(InventoryLot, lot_id)
    if state is None or lot is None:
        raise WarehouseInventoryError("库存计划状态不存在，请刷新后重试", 409)
    if state["external_version"] != expected_version:
        raise WarehouseInventoryError("同一库存批次的草稿版本不一致", 409)
    if lot.version != state["current_version"]:
        raise WarehouseInventoryError("库存已被其他请求修改，整单保存已取消", 409)
    return lot, state["current_version"]


def _advance_plan_version(
    db: Session,
    *,
    lot_id: int,
    states: dict[int, dict[str, int]],
) -> None:
    lot = db.get(InventoryLot, lot_id)
    if lot is None:
        raise WarehouseInventoryError("库存批次不存在", 409)
    states[lot_id]["current_version"] = lot.version


def _is_telescoping_product(product: Product) -> bool:
    value = (product.box_style or "").strip().upper()
    return bool(value) and ("天地盖" in value or "A3" in value)


def _semi_component_specs(
    item: OrderItem,
    product: Product,
) -> list[dict[str, object]]:
    if _is_telescoping_product(product):
        return [
            {
                "component_type": "cover",
                "board_length_mm": item.snapshot_report_length_mm,
                "board_width_mm": item.snapshot_report_width_mm,
                "pieces_per_box": 1,
            },
            {
                "component_type": "base",
                "board_length_mm": item.snapshot_base_report_length_mm,
                "board_width_mm": item.snapshot_base_report_width_mm,
                "pieces_per_box": 1,
            },
        ]
    return [
        {
            "component_type": "whole",
            "board_length_mm": item.snapshot_report_length_mm,
            "board_width_mm": item.snapshot_report_width_mm,
            "pieces_per_box": max(int(item.snapshot_pieces_per_box or 1), 1),
        }
    ]


def _apply_order_reservation_plans(
    db: Session,
    *,
    order: Order,
    payload_items: list[OrderItemCreate],
    created_items: list[OrderItem],
    resolved_products: dict[int, Product],
    states: dict[int, dict[str, int]],
    operator_id: int | None,
) -> None:
    for index, (item_payload, item) in enumerate(
        zip(payload_items, created_items, strict=True), start=1
    ):
        if is_composite_product(resolved_products[index]):
            continue
        plan = item_payload.reservation_plan
        if plan is None:
            continue
        for plan_index, entry in enumerate(plan.finished, start=1):
            lot, current_version = _plan_current_version(
                db,
                lot_id=entry.lot_id,
                expected_version=entry.expected_version,
                states=states,
            )
            remaining_boxes = max(
                item.quantity - active_finished_reserved_qty(db, item.id), 0
            )
            allocated_boxes = min(
                entry.requested_qty,
                remaining_boxes,
                lot.quantity_available,
            )
            if allocated_boxes <= 0:
                continue
            reserve_finished_inventory(
                db,
                order_item_id=item.id,
                inventory_lot_id=lot.id,
                quantity=allocated_boxes,
                expected_version=current_version,
                operator_id=operator_id,
                idempotency_key=_order_inventory_reservation_key(
                    order=order,
                    item=item,
                    item_payload=item_payload,
                    inventory_type="finished",
                    plan_index=plan_index,
                ),
                warning_acknowledged_codes=[],
            )
            _advance_plan_version(db, lot_id=lot.id, states=states)

    requirement_map: dict[tuple[int, str], object] = {}
    for index, (item_payload, item) in enumerate(
        zip(payload_items, created_items, strict=True), start=1
    ):
        product = resolved_products[index]
        if is_composite_product(product):
            continue
        plan = item_payload.reservation_plan or OrderItemReservationPlan()
        production_required_boxes = max(
            item.quantity - active_finished_reserved_qty(db, item.id), 0
        )
        if production_required_boxes <= 0:
            if plan.semi:
                raise WarehouseInventoryError(
                    f"第{index}条明细已被成品库存全额覆盖，不能再预占半成品", 409
                )
            continue
        specs = _semi_component_specs(item, product)
        allowed_components = {str(spec["component_type"]) for spec in specs}
        for entry in plan.semi:
            if entry.component_type not in allowed_components:
                raise WarehouseInventoryError(
                    f"第{index}条明细半成品组件与箱型不一致", 409
                )
        material_code = (item.snapshot_material or "").strip()
        flute_type = (item.flute_type or "").strip().upper()
        planned_by_component: dict[str, list[SemiReservationPlanEntry]] = {}
        for entry in plan.semi:
            planned_by_component.setdefault(entry.component_type, []).append(entry)
        for spec in specs:
            component_type = str(spec["component_type"])
            component_plans = planned_by_component.get(component_type, [])
            board_length_mm = spec["board_length_mm"]
            board_width_mm = spec["board_width_mm"]
            if not (
                board_length_mm
                and board_width_mm
                and material_code
                and flute_type
            ):
                if component_plans:
                    raise WarehouseInventoryError(
                        f"第{index}条明细缺少{component_type}半成品签名字段", 409
                    )
                continue
            stock_yield_per_sheet = 1
            if component_plans:
                planned_lot = db.get(InventoryLot, component_plans[0].lot_id)
                if planned_lot is None or planned_lot.semi_finished_detail is None:
                    raise WarehouseInventoryError("半成品库存批次不存在", 404)
                stock_yield_per_sheet = (
                    planned_lot.semi_finished_detail.stock_yield_per_sheet
                )
            pieces_per_box = int(spec["pieces_per_box"])
            requirement = save_order_item_semi_requirement(
                db,
                order_item_id=item.id,
                component_type=component_type,
                board_length_mm=int(board_length_mm),
                board_width_mm=int(board_width_mm),
                material_code=material_code,
                flute_type=flute_type,
                pieces_per_box=pieces_per_box,
                stock_yield_per_sheet=stock_yield_per_sheet,
                required_piece_quantity=production_required_boxes * pieces_per_box,
                operator_id=operator_id,
            )
            requirement_map[(item.id, component_type)] = requirement

    for index, (item_payload, item) in enumerate(
        zip(payload_items, created_items, strict=True), start=1
    ):
        if is_composite_product(resolved_products[index]):
            continue
        plan = item_payload.reservation_plan
        if plan is None:
            continue
        for plan_index, entry in enumerate(plan.semi, start=1):
            requirement = requirement_map.get((item.id, entry.component_type))
            if requirement is None:
                raise WarehouseInventoryError(
                    f"第{index}条明细无法建立{entry.component_type}半成品需求", 409
                )
            lot, current_version = _plan_current_version(
                db,
                lot_id=entry.lot_id,
                expected_version=entry.expected_version,
                states=states,
            )
            if lot.quantity_available <= 0:
                continue
            candidates = {
                candidate.lot.id: candidate
                for candidate in semi_finished_inventory_candidates(
                    db, requirement.id
                )
            }
            candidate = candidates.get(lot.id)
            if entry.recommendation_source == "general_signature":
                if (
                    candidate is None
                    or candidate.source != "general_signature"
                    or candidate.signature_differences
                ):
                    raise WarehouseInventoryError(
                        "general_signature 推荐签名已变化，请刷新", 409
                    )
                if (
                    GENERAL_SEMI_FINISHED_STOCK
                    not in entry.warning_acknowledged_codes
                ):
                    raise WarehouseInventoryError(
                        "通用半成品库存抵扣必须确认通用库存警告", 409
                    )
            elif entry.recommendation_source == "learned":
                if (
                    candidate is None
                    or candidate.source != "learned"
                    or candidate.match_rule_id != entry.match_rule_id
                ):
                    raise WarehouseInventoryError("learned 推荐规则已变化，请刷新", 409)
                if SIGNATURE_OVERRIDE_WARNING in candidate.warning_codes:
                    if not entry.override:
                        raise WarehouseInventoryError(
                            "learned 推荐存在已学习的签名差异，必须明确 override",
                            409,
                        )
                    if (
                        SIGNATURE_OVERRIDE_WARNING
                        not in entry.warning_acknowledged_codes
                    ):
                        raise WarehouseInventoryError(
                            "learned 推荐必须确认半成品签名差异警告", 409
                        )
            elif entry.recommendation_source == "signature":
                if candidate is None or candidate.signature_differences:
                    raise WarehouseInventoryError("signature 推荐签名已变化，请刷新", 409)
            else:
                browsed_ids = {
                    row.lot.id
                    for row in browse_semi_finished_inventory(db, requirement.id)
                }
                if lot.id not in browsed_ids or not entry.override:
                    raise WarehouseInventoryError("人工半成品匹配确认无效", 409)
            reserve_semi_finished_inventory(
                db,
                requirement_id=requirement.id,
                requested_requirement_quantity=entry.requested_qty,
                lots=[SemiFinishedLotVersion(lot.id, current_version)],
                operator_id=operator_id,
                idempotency_key=_order_inventory_reservation_key(
                    order=order,
                    item=item,
                    item_payload=item_payload,
                    inventory_type="semi",
                    plan_index=plan_index,
                ),
                confirmed=True,
                override=entry.override,
                warning_acknowledged_codes=entry.warning_acknowledged_codes,
            )
            _advance_plan_version(db, lot_id=lot.id, states=states)


def _order_inventory_reservation_key(
    *,
    order: Order,
    item: OrderItem,
    item_payload: OrderItemCreate,
    inventory_type: Literal["finished", "semi"],
    plan_index: int,
) -> str:
    """Build a stable key that cannot collide when SQLite reuses deleted IDs."""
    client_line_id = (item_payload.client_line_id or "").strip()
    line_identity = client_line_id or str(item.item_sequence or item.id)
    seed = "|".join(
        (
            order.order_number,
            line_identity,
            inventory_type,
            str(plan_index),
        )
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:48]
    return f"order-create-{inventory_type}-{digest}"


class OrderUpdate(BaseModel):
    customer_po: str | None = None
    delivery_date: date | None = None
    remark: str | None = None


class DraftRematchRequest(BaseModel):
    draft: dict
    customer_id: int
    preview_safety_token: str = Field(min_length=1, max_length=4000)


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


def _completion_dates_by_item(
    db: Session,
    item_ids: list[int],
) -> dict[int, date]:
    if not item_ids:
        return {}
    return {
        int(item_id): completion_date
        for item_id, completion_date in db.execute(
            select(
                DeliveryItem.order_item_id,
                func.max(Delivery.delivery_date),
            )
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(
                DeliveryItem.order_item_id.in_(item_ids),
                Delivery.status == "dispatched",
            )
            .group_by(DeliveryItem.order_item_id)
        ).all()
        if completion_date is not None
    }


def _order_response(
    order: Order,
    user: User,
    *,
    db: Session | None = None,
    customer_name: str | None = None,
    display_registry=None,
    completion_dates: dict[int, date] | None = None,
    bom_components_by_item_id: dict[int, list[dict]] | None = None,
) -> dict:
    item_ids = [item.id for item in order.items]
    reservation_map = (
        active_finished_reservations_by_item_ids(
            db, item_ids
        )
        if db is not None
        else {}
    )
    completion_date_map = completion_dates
    if completion_date_map is None and db is not None:
        completion_date_map = _completion_dates_by_item(
            db, item_ids
        )
    completion_date_map = completion_date_map or {}
    if bom_components_by_item_id is None:
        bom_components_by_item_id = (
            get_order_item_bom_components_by_item_ids(db, item_ids)
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
        mold_tool = (
            item.product.mold_tool
            if item.product_id
            and item.product is not None
            and "模切"
            in {
                value.strip()
                for value in re.split(
                    r"[,，、]", str(item.product.production_process or "")
                )
                if value.strip()
            }
            else None
        )
        finished_reserved_quantity = reservation_map.get(item.id, 0)
        production_required_quantity = max(
            item.quantity - finished_reserved_quantity, 0
        )
        may_view_cost = has_permission(user, "cost.view")
        cost_reference = (
            calculate_draft_cost(db, item.product_id, item.material_id)
            if db is not None and may_view_cost
            else {}
        )
        item_data = {
                "id": item.id,
                "product_id": item.product_id,
                "item_order_number": item.item_order_number,
                "item_sequence": item.item_sequence,
                "quantity": item.quantity,
                "ordered_quantity": item.quantity,
                "bom_components": bom_components_by_item_id.get(item.id, []),
                "delivered_quantity": item.delivered_quantity,
                "remaining_quantity": max(
                    int(item.quantity or 0) - int(item.delivered_quantity or 0),
                    0,
                ),
                "completion_date": (
                    completion_date_map.get(item.id)
                    if int(item.quantity or 0) > 0
                    and int(item.delivered_quantity or 0) >= int(item.quantity or 0)
                    else None
                ),
                "is_force_closed": item.is_force_closed,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal,
                "material_status": item.material_status,
                "material_received_at": (
                    utc_naive_to_api(item.material_received_at)
                    if item.material_received_at
                    else None
                ),
                "snapshot_product_code": item.snapshot_product_code,
                "snapshot_product_name": item.snapshot_product_name,
                "snapshot_spec": item.snapshot_spec,
                "snapshot_material": item.snapshot_material,
                "snapshot_original_material_code": item.snapshot_original_material_code,
                "snapshot_customer_model": item.snapshot_customer_model,  # v0.19.1
                "snapshot_production_notes": item.snapshot_production_notes,  # v0.19.2-A
                "display_material": _display_material(item.snapshot_material),
                # v0.19.2-B: 常用箱层数/楞型/供应商/克重/图纸
                "layer_count": item.layer_count,
                "flute_type": item.flute_type,
                "material_id": item.material_id,
                "snapshot_supplier_name": item.snapshot_supplier_name,
                "snapshot_weight": item.snapshot_weight,
                "drawing_file": _order_drawing_url(item.id, item.drawing_file),
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
                    _product_drawing_url(item.product.drawings[0])
                    if item.product_id
                    and item.product is not None
                    and item.product.drawings
                    else None
                ),
                "mold_tool_id": mold_tool.id if mold_tool else None,
                "mold_code": mold_tool.mold_code if mold_tool else None,
                "mold_name": mold_tool.mold_name if mold_tool else None,
                "mold_location": mold_tool.rack_location if mold_tool else None,
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
                "supplier_delivery_time": (
                    beijing_naive_to_api(item.supplier_delivery_time)
                    if item.supplier_delivery_time
                    else None
                ),
                **cost_reference,
        }
        if may_view_cost and item_data.get("estimated_cost") is not None:
            unit_cost = Decimal(item_data["estimated_cost"])
            item_data.update(
                _order_item_cost_totals(
                    item.quantity,
                    Decimal(str(item.unit_price)),
                    Decimal(str(item.subtotal)),
                    unit_cost,
                )
            )
        elif may_view_cost:
            item_data.update(
                {
                    "unit_estimated_cost": None,
                    "unit_estimated_gross_profit": None,
                    "sale_amount": str(Decimal(str(item.subtotal)).quantize(MONEY_QUANTUM)),
                    "total_estimated_cost": None,
                    "total_estimated_gross_profit": None,
                }
            )
        else:
            item_data["sale_amount"] = str(
                Decimal(str(item.subtotal)).quantize(MONEY_QUANTUM)
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
    sort_by: Literal["customer_name", "order_date", "delivery_date"] | None = None,
    sort_direction: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    display_registry = build_display_registry(db)
    search_keyword = keyword.strip() if keyword and keyword.strip() else None
    ids_query = (
        select(Order.id)
        .join(Customer, Customer.id == Order.customer_id)
        .distinct()
    )
    joined_items = False

    scoped_customer_ids = customer_scope_ids(user, db)
    is_customer_scope_restricted = not has_unrestricted_customer_access(user, db)
    if is_customer_scope_restricted:
        ids_query = ids_query.where(Order.customer_id.in_(scoped_customer_ids))

    if customer_id is not None:
        require_customer_access(customer_id, current_user=user, db=db)
        ids_query = ids_query.where(Order.customer_id == customer_id)

    if customer_name and customer_name.strip():
        ids_query = ids_query.where(
            Customer.name.ilike(f"%{customer_name.strip()}%")
        )

    if order_date is not None:
        ids_query = ids_query.where(Order.order_date == order_date)
    if date_from is not None:
        ids_query = ids_query.where(Order.order_date >= date_from)
    if date_to is not None:
        ids_query = ids_query.where(Order.order_date <= date_to)

    history_condition = Order.order_number.like("RUIDA-%")
    fully_delivered_condition = and_(
        Order.items.any(),
        ~Order.items.any(OrderItem.delivered_quantity < OrderItem.quantity),
    )
    if status_filter == "business":
        business_conditions = [
            ~history_condition,
            Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
        ]
        # A targeted business search must still find normally completed
        # deliveries. Without a search term, keep the operational list focused
        # on orders that still need attention.
        if not search_keyword:
            business_conditions.append(~fully_delivered_condition)
        ids_query = ids_query.where(*business_conditions)
    elif status_filter == "history":
        ids_query = ids_query.where(history_condition)
    elif status_filter == "finished_delivery":
        ids_query = ids_query.where(
            ~history_condition,
            Order.status.notin_(("dead", "cancelled", "closed", "archived")),
            fully_delivered_condition,
        )
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

    if search_keyword:
        trimmed = search_keyword
        display_ids = filter_order_ids_for_display_search(db, trimmed, display_registry)
        ids_query = ids_query.outerjoin(
            OrderItem, OrderItem.order_id == Order.id
        ).outerjoin(
            Product, Product.id == OrderItem.product_id
        ).where(
            or_(
                Order.order_number.ilike(f"%{trimmed}%"),
                Order.customer_po.ilike(f"%{trimmed}%"),
                Customer.name.ilike(f"%{trimmed}%"),
                OrderItem.item_order_number.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_product_code.ilike(f"%{trimmed}%"),
                Product.product_code.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_product_name.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_spec.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_material.ilike(f"%{trimmed}%"),
                Order.id.in_(display_ids) if display_ids else False,
            )
        )
        joined_items = True

    if order_number and order_number.strip():
        trimmed = order_number.strip()
        display_ids = filter_order_ids_for_display_search(db, trimmed, display_registry)
        if not joined_items:
            ids_query = ids_query.outerjoin(
                OrderItem, OrderItem.order_id == Order.id
            )
        ids_query = ids_query.where(
            or_(
                Order.order_number == trimmed,
                OrderItem.item_order_number == trimmed,
                Order.id.in_(display_ids) if display_ids else False,
            )
        )

    # Explicit table-header sorting is constrained to a fixed whitelist above.
    if sort_by:
        sort_column = {
            "customer_name": Customer.name,
            "order_date": Order.order_date,
            "delivery_date": Order.delivery_date,
        }[sort_by]
        primary_sort = (
            sort_column.asc() if sort_direction == "asc" else sort_column.desc()
        )
        tie_breaker = Order.id.asc() if sort_direction == "asc" else Order.id.desc()
        if sort_by == "delivery_date":
            ids_query = ids_query.order_by(
                Order.delivery_date.is_(None), primary_sort, tie_breaker
            )
        else:
            ids_query = ids_query.order_by(primary_sort, tie_breaker)
    # History (RUIDA legacy) keeps chronological order_date ordering. All other
    # views — especially "business" — sort by creation time so a freshly saved
    # order surfaces at the top even when its order_date is back-dated to the
    # source document date (e.g. PDF imports), instead of being buried below
    # newer-dated rows where users assume it "disappeared".
    elif status_filter == "history":
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
                ),
                selectinload(Order.items).selectinload(OrderItem.product).selectinload(
                    Product.mold_tool  # type: ignore[attr-defined]
                ),
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
    completion_dates = _completion_dates_by_item(
        db,
        [item.id for order in orders for item in order.items],
    )
    bom_components_by_item_id = get_order_item_bom_components_by_item_ids(
        db,
        [item.id for order in orders for item in order.items],
    )
    unfinished_query = select(func.count(Order.id)).where(
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
    if is_customer_scope_restricted:
        unfinished_query = unfinished_query.where(
            Order.customer_id.in_(scoped_customer_ids)
        )
    unfinished_total = db.scalar(unfinished_query) or 0
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
                completion_dates=completion_dates,
                bom_components_by_item_id=bom_components_by_item_id,
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
    preview_date = order_date or beijing_today()
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
    """Preview via the shared pure parse/OCR pipeline."""
    return parse_pdf_bytes(content, filename, template_rules).draft


def _customer_id_value(value: object) -> int | None:
    try:
        customer_id = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return customer_id if customer_id > 0 else None


def _scope_denied_pdf_preview(draft: dict) -> dict:
    """Return a fixed, customer-data-free preview for an out-of-scope match.

    Build this payload from a whitelist instead of redacting the matched draft.
    This keeps future customer/product fields fail-closed as the PDF parser grows.
    """

    return {
        "source_name": str(draft.get("source_name") or "uploaded.pdf"),
        "file_hash": str(draft.get("file_hash") or ""),
        "source_type": "purchase_order_pdf",
        "recognition_status": "needs_confirmation",
        "parse_status": "needs_confirmation",
        "duplicate_status": None,
        "item_count": 0,
        "items": [],
        "warnings": ["当前账号无权访问该 PDF 对应客户，请联系管理员分配客户范围。"],
        "customer_route": {"status": "needs_confirmation"},
        "integrity_check": {
            "integrity_status": "unknown",
            "integrity_errors": [],
            "integrity_warnings": ["客户范围未通过，未执行业务数据匹配。"],
        },
    }


def _match_pdf_customer_in_scope(
    db: Session,
    raw_name: str | None,
    visible_customer_ids: set[int],
) -> tuple[str, int | None, list[dict]]:
    """Resolve a parsed customer name without reading outside the allow-list."""

    if not raw_name or not visible_customer_ids:
        return "unmatched", None, []
    full_target = _full_company_name_key(raw_name)
    target = _company_name_key(raw_name)
    exact_candidates: list[dict] = []
    normalized_candidates: list[dict] = []
    partial_candidates: list[dict] = []
    customers = db.scalars(
        select(Customer)
        .where(
            Customer.id.in_(visible_customer_ids),
            Customer.is_active.is_(True),
        )
        .order_by(Customer.id)
    ).all()
    for customer in customers:
        candidate = {"id": customer.id, "name": customer.name}
        full_key = _full_company_name_key(customer.name)
        key = _company_name_key(customer.name)
        if full_key and full_key == full_target:
            exact_candidates.append(candidate)
        elif key and key == target:
            normalized_candidates.append(candidate)
        elif key and target and (key in target or target in key):
            partial_candidates.append(candidate)
    candidates = exact_candidates or normalized_candidates or partial_candidates
    if len(candidates) == 1:
        return "matched", candidates[0]["id"], candidates
    if len(candidates) > 1:
        return "multiple_candidates", None, candidates
    return "unmatched", None, []


def _match_pdf_preview_for_user(
    db: Session,
    draft: dict,
    user: User,
    *,
    customer_id: int | None = None,
) -> dict:
    """Apply the same customer scope gate to preview, batch and rematch.

    Restricted users are resolved against their allow-list before
    ``match_import_draft`` can query customer products or material candidates.
    """

    if customer_id is not None:
        require_customer_access(customer_id, current_user=user, db=db)
        if db.get(Customer, customer_id) is None:
            raise HTTPException(status_code=400, detail="客户不存在")
        return match_import_draft(db, draft, customer_id=customer_id)

    if has_unrestricted_customer_access(user, db):
        return match_import_draft(db, draft)

    visible_customer_ids = customer_scope_ids(user, db)
    if not visible_customer_ids:
        return _scope_denied_pdf_preview(draft)

    route = (
        draft.get("customer_route")
        if isinstance(draft.get("customer_route"), dict)
        else {}
    )
    route_status = route.get("status")
    if route_status == "locked":
        routed_customer_id = _customer_id_value(route.get("template_customer_id"))
        if routed_customer_id is not None:
            if routed_customer_id not in visible_customer_ids:
                return _scope_denied_pdf_preview(draft)
            routed_customer = db.scalar(
                select(Customer).where(
                    Customer.id == routed_customer_id,
                    Customer.is_active.is_(True),
                    Customer.status == "active",
                )
            )
            if routed_customer is None:
                return _scope_denied_pdf_preview(draft)
            return match_import_draft(db, draft, customer_id=routed_customer_id)
        match_status, matched_customer_id, _candidates = _match_pdf_customer_in_scope(
            db,
            draft.get("customer_name_raw") or draft.get("customer_name"),
            visible_customer_ids,
        )
        if match_status != "matched" or matched_customer_id is None:
            return _scope_denied_pdf_preview(draft)
        return match_import_draft(db, draft, customer_id=matched_customer_id)

    if route_status == "needs_confirmation":
        route_candidates = route.get("candidates")
        if isinstance(route_candidates, list):
            visible_candidates = [
                candidate
                for candidate in route_candidates
                if isinstance(candidate, dict)
                and _customer_id_value(candidate.get("template_customer_id"))
                in visible_customer_ids
            ]
            if route_candidates and not visible_candidates:
                return _scope_denied_pdf_preview(draft)
            draft = {
                **draft,
                "customer_route": {**route, "candidates": visible_candidates},
            }
        return match_import_draft(db, draft)

    match_status, matched_customer_id, candidates = _match_pdf_customer_in_scope(
        db,
        draft.get("customer_name_raw") or draft.get("customer_name"),
        visible_customer_ids,
    )
    if matched_customer_id is not None:
        if matched_customer_id not in visible_customer_ids:
            return _scope_denied_pdf_preview(draft)
        return match_import_draft(db, draft, customer_id=matched_customer_id)

    if match_status == "multiple_candidates":
        visible_candidates = [
            candidate
            for candidate in candidates
            if _customer_id_value(candidate.get("id")) in visible_customer_ids
        ]
        if not visible_candidates:
            return _scope_denied_pdf_preview(draft)
        draft = {
            **draft,
            "customer_route": {
                "status": "needs_confirmation",
                "candidates": [
                    {
                        "template_customer_id": candidate["id"],
                        "customer_name": candidate["name"],
                    }
                    for candidate in visible_candidates
                ],
            },
        }
        return match_import_draft(db, draft)
    return _scope_denied_pdf_preview(draft)


@router.post("/pdf-preview")
async def preview_order_pdf(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    try:
        upload = await read_validated_upload(file, PDF_POLICY)
    except UploadValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    filename = upload.original_filename
    content = upload.content
    template_rules = load_active_pdf_template_rules(db)
    try:
        draft = _parse_order_pdf_preview(content, filename, template_rules)
        draft["file_hash"] = file_sha256(content)
        return _finalize_pdf_preview_for_user(
            _match_pdf_preview_for_user(db, draft, user),
            user,
        )
    except PdfParseError as error:
        return _finalize_pdf_preview_for_user(
            _pdf_failure_draft(filename, error, digest=file_sha256(content)),
            user,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail="文件识别失败，请检查文件内容后重试") from error


@router.post("/pdf-preview-batch")
async def preview_order_pdf_batch(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个 PDF 文件")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="单次最多上传 20 个 PDF 文件")
    drafts: list[dict] = []
    seen_hashes: set[str] = set()
    template_rules = load_active_pdf_template_rules(db)
    request_total_bytes = 0
    for file in files:
        filename = (file.filename or "uploaded.pdf").strip()
        digest = ""
        try:
            upload = await read_validated_upload(file, PDF_POLICY)
            filename = upload.original_filename
            content = upload.content
            request_total_bytes += upload.size
            if request_total_bytes > 100 * 1024 * 1024:
                raise ValueError("单次请求文件总大小不能超过 100MB")
            digest = upload.sha256
            if digest in seen_hashes:
                drafts.append(
                    _finalize_pdf_preview_for_user(
                        {
                            "source_name": filename,
                            "file_hash": digest,
                            "recognition_status": "duplicate_skipped",
                            "duplicate_status": "duplicate_skipped",
                            "duplicate_reason": "本批次已上传相同文件",
                            "items": [],
                            "warnings": ["本批次已上传相同文件，已跳过。"],
                        },
                        user,
                    )
                )
                continue
            seen_hashes.add(digest)
            draft = _parse_order_pdf_preview(content, filename, template_rules)
            draft["file_hash"] = digest
            drafts.append(
                _finalize_pdf_preview_for_user(
                    _match_pdf_preview_for_user(db, draft, user), user
                )
            )
        except PdfParseError as error:
            drafts.append(
                _finalize_pdf_preview_for_user(
                    _pdf_failure_draft(filename, error, digest=digest),
                    user,
                )
            )
        except ValueError as error:
            drafts.append(
                _finalize_pdf_preview_for_user(
                    {
                        "source_name": filename,
                        "file_hash": digest,
                        "recognition_status": "failed",
                        "duplicate_status": None,
                        "items": [],
                        "warnings": [str(error)],
                    },
                    user,
                )
            )
        except Exception:
            drafts.append(
                _finalize_pdf_preview_for_user(
                    {
                        "source_name": filename,
                        "file_hash": digest,
                        "recognition_status": "failed",
                        "duplicate_status": None,
                        "items": [],
                        "warnings": ["文件识别失败，请检查文件内容后重试。"],
                    },
                    user,
                )
            )
    return {"batch_count": len(files), "drafts": drafts}


@router.post("/draft-rematch")
def rematch_order_draft(
    payload: DraftRematchRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(can_create),
) -> dict:
    trusted_claims = _decode_pdf_preview_safety_token(
        payload.preview_safety_token,
        _user,
    )
    draft_source_name = str(payload.draft.get("source_name") or "").strip()
    trusted_source_name = str(trusted_claims["source_name"] or "").strip()
    draft_source_hash = str(payload.draft.get("file_hash") or "").strip().casefold()
    trusted_source_hash = str(trusted_claims["source_hash"] or "").strip().casefold()
    if (
        draft_source_name or trusted_source_name
    ) and draft_source_name != trusted_source_name:
        raise _pdf_preview_token_error("PDF 预览 token 与草稿文件名不一致，请重新预览")
    if (
        draft_source_hash or trusted_source_hash
    ) and draft_source_hash != trusted_source_hash:
        raise _pdf_preview_token_error("PDF 预览 token 与草稿文件哈希不一致，请重新预览")
    trusted_draft = dict(payload.draft)
    trusted_draft["source_name"] = trusted_claims["source_name"]
    trusted_draft["file_hash"] = trusted_claims["source_hash"]
    trusted_draft["recognition_status"] = trusted_claims["recognition_status"]
    trusted_draft["parse_status"] = trusted_claims["recognition_status"]
    route = (
        dict(trusted_draft.get("customer_route"))
        if isinstance(trusted_draft.get("customer_route"), dict)
        else {}
    )
    route["status"] = trusted_claims["customer_route_status"]
    trusted_draft["customer_route"] = route
    integrity = (
        dict(trusted_draft.get("integrity_check"))
        if isinstance(trusted_draft.get("integrity_check"), dict)
        else {}
    )
    integrity["integrity_status"] = trusted_claims["integrity_status"]
    trusted_draft["integrity_check"] = integrity
    result = _match_pdf_preview_for_user(
        db,
        trusted_draft,
        _user,
        customer_id=payload.customer_id,
    )
    return _finalize_pdf_preview_for_user(
        result,
        _user,
        state_overrides={
            "recognition_status": trusted_claims["recognition_status"],
            "customer_route_status": trusted_claims["customer_route_status"],
            "customer_match_status": "matched",
            "integrity_status": trusted_claims["integrity_status"],
            "matched_customer_id": payload.customer_id,
        },
    )


@router.post("/cost-preview")
def preview_order_cost(
    payload: CostPreviewRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(can_view_cost),
) -> dict:
    """纸板成本预估；若传 flute_type 则计入楞型加价（v0.19.2-B）。"""
    product_for_scope = db.get(Product, payload.product_id)
    if product_for_scope is None:
        raise HTTPException(status_code=404, detail="常用箱不存在")
    require_customer_access(
        product_for_scope.customer_id, current_user=_user, db=db
    )
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
    """Store a validated draft outside static and return only a short-lived token."""
    try:
        upload = await read_validated_upload(file, DRAWING_POLICY)
    except UploadValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    token = create_temporary_token(upload, owner_id=user.id)
    db.add(
        OperationLog(
            user_id=user.id,
            action="UPLOAD_DRAFT_DRAWING",
            resource="OrderDraftDrawing",
            details=json.dumps(
                {
                    "original_filename": upload.original_filename,
                    "content_type": upload.content_type,
                    "size": upload.size,
                    "sha256": upload.sha256,
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="order_draft_drawing",
            description="上传订单临时图纸",
        )
    )
    db.commit()
    return {"token": token}


@router.get("/draft-drawing/{token}/content/{display_name}")
def preview_draft_drawing(
    token: str,
    display_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> FileResponse:
    del display_name
    try:
        stored = temporary_token_file(token, owner_id=user.id)
    except UploadTokenError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    db.add(
        OperationLog(
            user_id=user.id,
            action="VIEW_DRAFT_DRAWING",
            resource="OrderDraftDrawing",
            details=json.dumps({"sha256": stored.sha256}, ensure_ascii=False),
            username=user.username,
            role=user.role,
            entity_type="order_draft_drawing",
            description="预览订单临时图纸",
        )
    )
    db.commit()
    return FileResponse(
        stored.path,
        media_type=stored.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/items/{item_id}/drawing", status_code=status.HTTP_200_OK)
async def upload_order_item_drawing(
    item_id: int,
    file: UploadFile = File(...),
    save_to_product: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    """上传订单明细图纸。默认只写 order_item；save_to_product=true 时同步写入 product_drawings（需用户确认）。"""
    if save_to_product:
        _require_product_drawing_edit(user)
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    try:
        upload = await read_validated_upload(file, DRAWING_POLICY)
        saved = save_product_drawing_files(product_id=item.product_id or 0, upload=upload)
    except (DrawingValidationError, UploadValidationError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    item.drawing_file = saved.image_path
    if save_to_product and item.product_id:
        from app.models.product_drawing import ProductDrawing
        product_drawing = ProductDrawing(
            product_id=item.product_id,
            image_path=item.drawing_file,
            thumbnail_path=item.drawing_file,
            uploaded_by=user.id,
        )
        db.add(product_drawing)
    db.add(
        OperationLog(
            user_id=user.id,
            action="UPLOAD_ORDER_DRAWING",
            resource="OrderItem",
            details=json.dumps(
                {
                    "original_filename": upload.original_filename,
                    "content_type": upload.content_type,
                    "size": upload.size,
                    "sha256": upload.sha256,
                    "saved_to_product": bool(save_to_product and item.product_id),
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=item.id,
            description="上传订单明细图纸",
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        remove_drawing_files(saved.image_path, saved.thumbnail_path)
        raise
    return {
        "drawing_file": _order_drawing_url(item.id, item.drawing_file),
        "saved_to_product": save_to_product,
    }


@router.get("/items/{item_id}/drawing/content/{display_name}")
def download_order_item_drawing(
    item_id: int,
    display_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> FileResponse:
    del display_name
    item = db.get(OrderItem, item_id)
    if item is None or not item.drawing_file:
        raise HTTPException(status_code=404, detail="订单图纸不存在")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    try:
        path = resolve_stored_reference(item.drawing_file)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="订单图纸文件不存在") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="订单图纸文件不存在")
    metadata = stored_file_metadata(path)
    content_type = str(
        metadata.get("content_type")
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    db.add(
        OperationLog(
            user_id=user.id,
            action="VIEW_ORDER_DRAWING",
            resource="OrderItem",
            details=json.dumps({"order_id": order.id}, ensure_ascii=False),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=item.id,
            description="查看订单明细图纸",
        )
    )
    db.commit()
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
    has_posted_incoming = bool(
        db.scalar(
            select(func.count())
            .select_from(IncomingReceiptItem)
            .where(
                IncomingReceiptItem.order_id.in_(order_ids),
                IncomingReceiptItem.status == "posted",
            )
        )
    )
    if has_posted_incoming:
        labels.append("有效来料实收")
    elif db.scalar(
        select(func.count())
        .select_from(IncomingReceiptItem)
        .where(IncomingReceiptItem.order_id.in_(order_ids))
    ):
        labels.append("已撤销来料审计")
    if db.scalar(
        select(func.count())
        .select_from(DeliveryItem)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(OrderItem.order_id.in_(order_ids))
    ):
        labels.append("送货单")
    if db.scalar(
        select(func.count())
        .select_from(RequisitionItem)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(
            OrderItem.order_id.in_(order_ids),
            or_(
                RequisitionItem.status == "已入库",
                OrderItem.material_status == "received",
            ),
        )
    ):
        labels.append("来料入库")
    if db.scalar(
        select(func.count())
        .select_from(SupplierRequisitionOrderItem)
        .join(OrderItem, OrderItem.id == SupplierRequisitionOrderItem.order_item_id)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .where(
            OrderItem.order_id.in_(order_ids),
            func.lower(func.trim(SupplierRequisitionOrder.status)).notin_(
                _INACTIVE_SUPPLIER_REQUISITION_ORDER_STATUSES
            ),
        )
    ):
        labels.append("供应商报料单")

    production_statuses = set(
        db.scalars(
            select(ProductionCompletion.status)
            .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
            .where(OrderItem.order_id.in_(order_ids))
            .distinct()
        ).all()
    )
    if "posted" in production_statuses:
        labels.append("生产完工")
    elif production_statuses:
        labels.append("已撤销生产审计")

    if _active_predelivery_order_ids(db, order_ids):
        labels.append("有效预送货")
    return labels


def _flow_delete_message(labels: list[str]) -> str:
    if "有效来料实收" in labels:
        return "该订单存在有效来料实收记录，不能物理删除。请先撤销来料，再将订单标记为作废或归档。"
    if "已撤销来料审计" in labels:
        return "该订单存在已撤销来料审计记录，不能物理删除。请将订单标记为作废或归档。"
    if "生产完工" in labels:
        return "该订单存在有效生产完工记录，不能物理删除。请先撤销生产确认，再将订单标记为作废或归档。"
    if "已撤销生产审计" in labels:
        return "该订单存在已撤销生产审计记录，不能物理删除。请将订单标记为作废或归档。"
    if "有效预送货" in labels:
        return "该订单仍存在有效预送货流程，请先撤回或作废预送货后再删除。"
    if "供应商报料单" in labels:
        return "该订单已生成供应商报料单，不能直接删除。"
    if "来料入库" in labels:
        return "该订单已来料入库，不能直接删除。"
    if "送货单" in labels:
        return "该订单已送货，不能直接删除。"
    flow_text = "/".join(dict.fromkeys(labels))
    return f"该订单已进入{flow_text}流程，不能直接删除。"


def _ensure_no_active_incoming_receipts(
    db: Session,
    *,
    order_id: int,
    order_item_ids: list[int],
) -> None:
    incoming_scope = IncomingReceiptItem.order_id == order_id
    if order_item_ids:
        incoming_scope = or_(
            incoming_scope,
            IncomingReceiptItem.order_item_id.in_(order_item_ids),
        )
    active_receipt_item_id = db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            incoming_scope,
            IncomingReceiptItem.status == "posted",
        )
        .limit(1)
    )
    if active_receipt_item_id is not None:
        raise HTTPException(
            status_code=409,
            detail="订单或明细已有有效来料实收记录，请先撤销来料后再撤回订单流程。",
        )


def _normalized_supplier_requisition_status(value: str | None) -> str:
    return str(value or "").strip().lower()


def _rollback_supplier_requisition_items(
    db: Session,
    *,
    order_item_ids: list[int],
) -> list[dict]:
    """撤回订单自己的供应商报料来源，不破坏共享报料单中的其他订单。"""
    if not order_item_ids:
        return []
    rows = db.execute(
        select(SupplierRequisitionOrderItem, SupplierRequisitionOrder)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .where(SupplierRequisitionOrderItem.order_item_id.in_(order_item_ids))
        .order_by(
            SupplierRequisitionOrderItem.supplier_order_id,
            SupplierRequisitionOrderItem.id,
        )
    ).all()
    grouped: dict[int, tuple[SupplierRequisitionOrder, list[SupplierRequisitionOrderItem]]] = {}
    for source_item, supplier_order in rows:
        grouped.setdefault(supplier_order.id, (supplier_order, []))[1].append(source_item)

    inactive_statuses = {
        _normalized_supplier_requisition_status(status)
        for status in _INACTIVE_SUPPLIER_REQUISITION_ORDER_STATUSES
    }
    changes: list[dict] = []
    for supplier_order, source_items in grouped.values():
        normalized_status = _normalized_supplier_requisition_status(
            supplier_order.status
        )
        if normalized_status in inactive_statuses:
            changes.append(
                {
                    "supplier_order_id": supplier_order.id,
                    "supplier_order_number": supplier_order.order_number,
                    "action": "already_inactive",
                    "status": supplier_order.status,
                    "source_item_ids": [item.id for item in source_items],
                }
            )
            continue
        if normalized_status not in _ROLLBACK_EDITABLE_SUPPLIER_REQUISITION_ORDER_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"供应商报料单 {supplier_order.order_number} 当前状态为"
                    f"{supplier_order.status or '未知'}，不能自动撤回订单来源。"
                ),
            )

        all_items = db.scalars(
            select(SupplierRequisitionOrderItem)
            .where(
                SupplierRequisitionOrderItem.supplier_order_id
                == supplier_order.id
            )
            .order_by(SupplierRequisitionOrderItem.id)
        ).all()
        source_ids = {item.id for item in source_items}
        remaining_items = [item for item in all_items if item.id not in source_ids]
        snapshot = [
            {
                "supplier_order_item_id": item.id,
                "order_item_id": item.order_item_id,
                "order_number": item.order_number,
                "product_code": item.product_code,
                "quantity": item.quantity,
                "stock_deduction_qty": item.stock_deduction_qty,
                "requisition_qty": item.requisition_qty,
                "required_piece_qty": item.required_piece_qty,
            }
            for item in source_items
        ]
        if not remaining_items:
            supplier_order.status = "voided"
            supplier_order.voided_at = supplier_order.voided_at or beijing_now_naive()
            action = "void_supplier_order"
        else:
            for source_item in source_items:
                db.delete(source_item)
            supplier_order.total_quantity = sum(
                int(item.quantity or 0) for item in remaining_items
            )
            supplier_order.stock_deduction_qty = sum(
                int(item.stock_deduction_qty or 0) for item in remaining_items
            )
            supplier_order.requisition_qty = sum(
                int(item.requisition_qty or 0) for item in remaining_items
            )
            required_values = [
                int(item.required_piece_qty)
                for item in remaining_items
                if item.required_piece_qty is not None
            ]
            supplier_order.required_piece_qty = (
                sum(required_values) if required_values else None
            )
            action = "remove_order_lines"
        changes.append(
            {
                "supplier_order_id": supplier_order.id,
                "supplier_order_number": supplier_order.order_number,
                "action": action,
                "source_items": snapshot,
                "remaining_item_count": len(remaining_items),
            }
        )
    return changes


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
        release_active_semi_reservations_for_items(
            db,
            order_item_ids=order_item_ids,
            operator_id=operator_id,
            reason=reason.replace("成品库存", "库存"),
            idempotency_prefix=f"{idempotency_prefix}-semi",
        )
    except WarehouseInventoryError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error


def _ensure_no_production_completion_facts(
    db: Session,
    order_item_ids: list[int],
) -> None:
    if order_item_ids and has_production_completion_facts(
        db,
        order_item_ids,
    ):
        raise HTTPException(status_code=409, detail=_PRODUCTION_FACT_CONFLICT)


def _lock_orders_for_production_transition(
    db: Session,
    order_ids: list[int],
) -> dict[int, Order]:
    try:
        return lock_order_rows_for_production_transition(db, order_ids)
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


def _production_meaning_changes(
    item: OrderItem,
    payload: OrderItemUpdate,
    product: Product | None,
) -> list[str]:
    changes: list[str] = []

    def changed(label: str, before: object, after: object) -> None:
        if after != before:
            changes.append(label)

    def clean(value: str | None) -> str:
        return (value or "").strip()

    changed("数量", int(item.quantity or 0), int(payload.quantity))
    changed("存货编码", clean(item.snapshot_product_code), clean(payload.product_code))
    changed("产品名称", clean(item.snapshot_product_name), clean(payload.product_name))
    changed("规格", clean(item.snapshot_spec), clean(payload.specification))
    changed("材质", clean(item.snapshot_material), clean(payload.material))
    if payload.production_notes is not None:
        changed(
            "生产说明",
            clean(item.snapshot_production_notes),
            clean(payload.production_notes),
        )
    if payload.material_id is not None:
        changed("材质", item.material_id, payload.material_id)
    if payload.layer_count is not None:
        changed("层数", item.layer_count, payload.layer_count)
    if payload.flute_type is not None:
        changed(
            "楞型",
            clean(item.flute_type).upper(),
            clean(payload.flute_type).upper(),
        )

    snapshot_fields = (
        "snapshot_report_length_mm",
        "snapshot_report_width_mm",
        "snapshot_crease_type",
        "snapshot_crease_left_mm",
        "snapshot_crease_middle_mm",
        "snapshot_crease_right_mm",
        "snapshot_report_notes",
        "snapshot_base_report_length_mm",
        "snapshot_base_report_width_mm",
        "snapshot_base_crease_type",
        "snapshot_base_crease_left_mm",
        "snapshot_base_crease_middle_mm",
        "snapshot_base_crease_right_mm",
        "snapshot_base_report_notes",
        "snapshot_splice_mode",
        "snapshot_pieces_per_box",
        "snapshot_flap_mm",
    )
    for field_name in snapshot_fields:
        value = getattr(payload, field_name)
        if value is not None:
            changed("生产快照", getattr(item, field_name), value or None)

    if payload.sync_product and product is not None:
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
                changed("产品生产参数", getattr(product, field_name), value)
    return list(dict.fromkeys(changes))


def _delete_orders_in_transaction(
    db: Session,
    *,
    orders: list[Order],
    user: User,
) -> None:
    order_ids = [order.id for order in orders]
    _lock_orders_for_production_transition(db, order_ids)
    orders = db.scalars(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id.in_(order_ids))
        .order_by(Order.id)
        .execution_options(populate_existing=True)
    ).all()
    if len(orders) != len(order_ids):
        raise HTTPException(status_code=409, detail="订单已被删除，请刷新后重试")
    item_ids = [item.id for order in orders for item in order.items]
    _ensure_no_production_completion_facts(db, item_ids)
    dependencies = _order_flow_dependencies(db, [order.id for order in orders])
    if dependencies:
        raise HTTPException(status_code=409, detail=_flow_delete_message(dependencies))
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
    if item_ids:
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
    user: User = Depends(can_status),
) -> dict:
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    target = payload.status.strip()
    remark = payload.remark.strip()
    if target not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="订单状态无效")
    if target in {"dead", "closed", "archived", "cancelled"} and not remark:
        raise HTTPException(status_code=400, detail="标记死单、已结档、已归档或已作废时必须填写备注")
    if target in {
        "pending_confirmation",
        "pending_production",
        "production",
        "dead",
        "cancelled",
        "closed",
        "archived",
    }:
        _lock_orders_for_production_transition(db, [order.id])
        order = db.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise HTTPException(status_code=409, detail="订单已被删除，请刷新后重试")
    if target in {
        "pending_confirmation",
        "pending_production",
        "production",
        "dead",
        "cancelled",
    }:
        _ensure_no_production_completion_facts(
            db,
            [item.id for item in order.items],
        )
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
    user: User = Depends(can_delete),
) -> Response:
    if not confirm:
        raise HTTPException(status_code=400, detail="删除订单需要二次确认")
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    _delete_orders_in_transaction(db, orders=[order], user=user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/group-delete")
def delete_order_group(
    payload: OrderGroupDeleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_delete),
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
    for order in orders:
        require_customer_access(order.customer_id, current_user=user, db=db)
    if len({_order_group_key(order) for order in orders}) != 1:
        raise HTTPException(status_code=400, detail="所选订单不属于同一订单组，请刷新后重试")
    _delete_orders_in_transaction(db, orders=orders, user=user)
    return {"deleted_count": len(orders)}


@router.put("/{order_id}/rollback-workflow")
def rollback_order_workflow(
    order_id: int,
    payload: WorkflowRollbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_rollback),
) -> dict:
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="撤回原因不能为空")
    order = db.scalar(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    _lock_orders_for_production_transition(db, [order.id])
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise HTTPException(status_code=409, detail="订单已被删除，请刷新后重试")
    if order.order_number.startswith("RUIDA-"):
        raise HTTPException(status_code=409, detail="历史订单禁止执行流程撤回")
    item_ids = [item.id for item in order.items]
    _ensure_no_production_completion_facts(db, item_ids)
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
    delivery_item_ids = [item.id for item in delivery_items]
    if delivery_item_ids and db.scalar(
        select(DeliveryInventoryAllocation.id)
        .where(
            DeliveryInventoryAllocation.delivery_item_id.in_(delivery_item_ids)
        )
        .limit(1)
    ) is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "送货单已产生库存出库记录，不能直接撤回订单流程。"
                "请先在送货管理中撤销发货并恢复库存。"
            ),
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
        _ensure_no_active_incoming_receipts(
            db,
            order_id=order.id,
            order_item_ids=item_ids,
        )
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
        supplier_requisition_changes = _rollback_supplier_requisition_items(
            db,
            order_item_ids=item_ids,
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
                        "supplier_requisition_changes": supplier_requisition_changes,
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
            ),
            selectinload(Order.items).selectinload(OrderItem.product).selectinload(
                Product.mold_tool  # type: ignore[attr-defined]
            ),
        )
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
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
    user: User = Depends(can_edit),
) -> dict:
    display_registry = build_display_registry(db)
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)

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
    if payload.customer_id is not None:
        require_customer_access(payload.customer_id, current_user=user, db=db)
    if payload.items is None:
        return _legacy_create(payload, user)
    if not payload.items:
        raise HTTPException(status_code=400, detail="订单至少需要一条明细")
    if payload.customer_id is None:
        raise HTTPException(status_code=400, detail="客户不能为空")
    pdf_safety_override_reasons, pdf_safety_claims = _validate_pdf_import_safety(
        payload,
        user,
    )
    if (
        any(
            item.drawing_save_option in _PRODUCT_DRAWING_SAVE_OPTIONS
            for item in payload.items
        )
        and not has_permission(user, "products.edit")
    ):
        _require_product_drawing_edit(user)
    if (
        any(
            item.drawing_save_option == "overwrite_product"
            for item in payload.items
        )
        and not has_permission(user, "products.delete")
    ):
        raise HTTPException(
            status_code=403,
            detail="无常用箱图纸覆盖权限",
        )
    if (
        payload.import_integrity_status == "failed"
        and payload.pdf_import_confirmation is None
    ):
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
        if payload.pdf_import_confirmation is not None and (
            not customer.is_active or customer.status != "active"
        ):
            raise HTTPException(status_code=400, detail="PDF 草稿所选客户已停用")

        customer_po = (payload.customer_po or "").strip() or None

        new_product_cache: dict[str, Product] = {}
        resolved_products: dict[int, Product] = {}
        validated_quantities: dict[int, int] = {}
        validated_layer_flutes: dict[
            int,
            tuple[int | None, str | None, int | None, Material | None],
        ] = {}
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
                            layer_count=item_payload.layer_count,
                            flute_type=item_payload.flute_type,
                        ),
                        cache=new_product_cache,
                        user=user,
                        reason="订单导入自动创建常用箱",
                        source="orders.create.product-import",
                    )
                except NewProductError as error:
                    raise HTTPException(
                        status_code=400, detail=f"第{index}条明细{error}"
                    ) from error
            crease_error = product_crease_width_error(product)
            if crease_error:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"第{index}条明细常用箱报料尺寸不一致：{crease_error}。"
                        "请先在常用箱中确认报料宽和压线尺寸后再下单"
                    ),
                )
            selected_material_id = (
                item_payload.material_id
                if item_payload.material_id is not None
                else product.material_id
            )
            selected_material = (
                db.get(Material, selected_material_id)
                if selected_material_id is not None
                else None
            )
            if selected_material_id is not None and selected_material is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细材质不存在",
                )
            if (
                selected_material is not None
                and item_payload.layer_count is not None
                and item_payload.layer_count != selected_material.layer_count
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细请求层数与所选材质真实层数不一致",
                )
            snapshot_layer_count = (
                selected_material.layer_count
                if selected_material is not None
                else (
                    item_payload.layer_count
                    if item_payload.layer_count is not None
                    else product.layer_count
                )
            )
            material_code_error = seven_layer_code_error(
                selected_material.code
                if selected_material is not None
                else (
                    (item_payload.material or "").strip()
                    or product.default_material_code
                    or product.legacy_material_text
                ),
                snapshot_layer_count,
            )
            if material_code_error:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细{material_code_error}",
                )
            snapshot_flute_type = _validated_order_layer_flute(
                snapshot_layer_count,
                (item_payload.flute_type or "").strip() or product.flute_type,
                detail_prefix=f"第{index}条明细",
            )
            validated_layer_flutes[index] = (
                snapshot_layer_count,
                snapshot_flute_type,
                selected_material_id,
                selected_material,
            )
            resolved_products[index] = product

        reservation_plan_states = _preflight_reservation_plans(
            db,
            customer_id=customer.id,
            payload_items=payload.items,
            resolved_products=resolved_products,
        )

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

        order_date = payload.order_date or beijing_today()
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
            (
                snapshot_layer_count,
                snapshot_flute_type,
                selected_material_id,
                selected_material,
            ) = validated_layer_flutes[index]

            subtotal = (
                Decimal(quantity) * unit_price
            ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            total += subtotal
            item_sequence = reserve_next_item_sequence(db, order.id)
            initial_material_code = (
                (item_payload.material or "").strip()
                or (
                    selected_material.code
                    if selected_material is not None
                    else product.legacy_material_text
                )
            )
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
                snapshot_material=initial_material_code,
                snapshot_original_material_code=initial_material_code,
                snapshot_customer_model=(
                    (item_payload.customer_model or "").strip() or None
                ),  # v0.19.1: TH型号 / 客户型号
                snapshot_production_notes=(
                    (item_payload.production_notes or "").strip() or None
                ),  # v0.19.2-A: 生产/印刷说明
                # v0.19.2-B: 常用箱层数/楞型/材质/供应商/克重 — 优先前端传值，否则从product取
                layer_count=snapshot_layer_count,
                flute_type=snapshot_flute_type,
                material_id=selected_material_id,
                snapshot_supplier_name=(
                    selected_material.supplier_name
                    if selected_material is not None
                    else None
                ),
                snapshot_weight=(
                    selected_material.basis_weight_description
                    if selected_material is not None
                    else None
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
                special_process=product.default_cutting_mode or "一开一",
                requisition_status="未报料",
            )
            # P0-B: the client can submit only a short-lived, owner-bound token.
            # A filesystem path is never interpreted from request data.
            if item_payload.temp_drawing_token:
                try:
                    stored_drawing = consume_temporary_token(
                        item_payload.temp_drawing_token,
                        owner_id=user.id,
                        category="drawings",
                    )
                except UploadTokenError as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
                item.drawing_file = stored_drawing.reference
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
        if pdf_safety_override_reasons:
            db.add(
                OperationLog(
                    user_id=user.id,
                    action="PDF_SAFETY_OVERRIDE",
                    resource="Order",
                    details=json.dumps(
                        {
                            "order_number": order.order_number,
                            "customer_id": order.customer_id,
                            "item_count": len(payload.items),
                            "source_name": pdf_safety_claims.get("source_name"),
                            "source_hash": pdf_safety_claims.get("source_hash"),
                            "recognition_status": pdf_safety_claims.get(
                                "recognition_status"
                            ),
                            "customer_route_status": pdf_safety_claims.get(
                                "customer_route_status"
                            ),
                            "integrity_status": pdf_safety_claims.get(
                                "integrity_status"
                            ),
                            "override_reasons": pdf_safety_override_reasons,
                        },
                        ensure_ascii=False,
                    ),
                    username=user.username,
                    role=user.role,
                    entity_type="order",
                    entity_id=order.id,
                    description="人工明确确认后保存存在安全闸门状态的 PDF 草稿",
                )
            )
        db.flush()  # 获取 item.id 以便处理图纸
        for index, created_item in enumerate(created_items, start=1):
            product = resolved_products[index]
            if is_composite_product(product):
                create_order_item_bom_snapshots(
                    db,
                    order_item=created_item,
                    parent_product=product,
                )
                create_or_refresh_production_task(db, created_item.id)
                continue
            create_or_refresh_production_task(db, created_item.id)
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
        _apply_order_reservation_plans(
            db,
            order=order,
            payload_items=payload.items,
            created_items=created_items,
            resolved_products=resolved_products,
            states=reservation_plan_states,
            operator_id=user.id,
        )
        for index, created_item in enumerate(created_items, start=1):
            if not is_composite_product(resolved_products[index]):
                refresh_production_task(db, created_item.id)
        refresh_order_production_status(db, order.id)
        db.commit()
        db.refresh(order)
        response = _order_response(
            order,
            user,
            db=db,
            customer_name=customer.name,
        )
        for response_item, request_item in zip(
            response["items"], payload.items, strict=True
        ):
            response_item["client_line_id"] = request_item.client_line_id
        return response
    except HTTPException:
        db.rollback()
        raise
    except CompositeBOMError as error:
        db.rollback()
        raise raise_composite_bom_http(error) from error
    except CompositeBomWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProductionWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="订单号或订单数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.get("/items/{item_id}/bom")
def read_order_item_bom(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    try:
        return get_order_item_bom_preview(db, item_id)
    except CompositeBOMError as error:
        raise raise_composite_bom_http(error) from error


@router.put("/items/{item_id}")
def update_order_item(
    item_id: int,
    payload: OrderItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    order_for_scope = db.get(Order, item.order_id)
    if order_for_scope is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(
        order_for_scope.customer_id, current_user=user, db=db
    )
    product_change_reason: str | None = None
    if payload.sync_product:
        if not has_permission(user, "products.edit"):
            _require_product_drawing_edit(user)
        if payload.product_expected_version is None:
            raise HTTPException(
                status_code=400,
                detail="同步常用箱必须提供 product_expected_version",
            )
        product_change_reason = (payload.product_change_reason or "").strip()
        if not product_change_reason:
            raise HTTPException(
                status_code=400,
                detail="同步常用箱必须填写 product_change_reason",
            )
    production_changes = _production_meaning_changes(
        item,
        payload,
        db.get(Product, item.product_id),
    )
    if production_changes and has_production_completion_facts(
        db,
        [item.id],
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "该订单明细已有生产完工或转库存事实，不能修改"
                + "、".join(production_changes)
                + "。"
            ),
        )
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
    if has_unconsumed_inventory_reservations(db, item.id):
        sensitive_changes = []
        if payload.quantity != item.quantity:
            sensitive_changes.append("数量")
        if (payload.material or "").strip() != (item.snapshot_material or "").strip():
            sensitive_changes.append("材质")
        if payload.material_id is not None and payload.material_id != item.material_id:
            sensitive_changes.append("材质")
        if (
            payload.flute_type is not None
            and payload.flute_type.strip().upper() != (item.flute_type or "").strip().upper()
        ):
            sensitive_changes.append("楞型")
        for label, field_name in (
            ("报料长", "snapshot_report_length_mm"),
            ("报料宽", "snapshot_report_width_mm"),
            ("底料长", "snapshot_base_report_length_mm"),
            ("底料宽", "snapshot_base_report_width_mm"),
            ("每箱片数", "snapshot_pieces_per_box"),
        ):
            value = getattr(payload, field_name)
            if value is not None and value != getattr(item, field_name):
                sensitive_changes.append(label)
        if (
            payload.snapshot_splice_mode is not None
            and payload.snapshot_splice_mode != item.snapshot_splice_mode
        ):
            sensitive_changes.append("拼版方式")
        product = db.get(Product, item.product_id)
        if (
            payload.box_style is not None
            and product is not None
            and payload.box_style != product.box_style
        ):
            sensitive_changes.append("产品箱型")
        if product is not None:
            for label, field_name in (
                ("产品长", "length_mm"),
                ("产品宽", "width_mm"),
                ("产品高", "height_mm"),
            ):
                value = getattr(payload, field_name)
                if value is not None and Decimal(value) != getattr(product, field_name):
                    sensitive_changes.append(label)
        if sensitive_changes:
            raise HTTPException(
                status_code=409,
                detail=(
                    "该订单明细已有未消耗库存预占，不能修改"
                    + "、".join(dict.fromkeys(sensitive_changes))
                    + "；请先释放库存预占。"
                ),
            )
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="数量必须大于0")
    unit_price = Decimal(str(payload.unit_price))
    if unit_price < 0:
        raise HTTPException(status_code=400, detail="单价不能为负数")
    order = db.get(Order, item.order_id)
    selected_material_id = (
        payload.material_id if payload.material_id is not None else item.material_id
    )
    selected_material = (
        db.get(Material, selected_material_id)
        if selected_material_id is not None
        else None
    )
    if selected_material_id is not None and selected_material is None:
        raise HTTPException(status_code=400, detail="订单明细材质不存在")
    if (
        selected_material is not None
        and payload.layer_count is not None
        and payload.layer_count != selected_material.layer_count
    ):
        raise HTTPException(status_code=400, detail="订单明细请求层数与所选材质真实层数不一致")
    prospective_item_layer = (
        selected_material.layer_count
        if selected_material is not None
        else (
            payload.layer_count
            if payload.layer_count is not None
            else item.layer_count
        )
    )
    material_code_error = seven_layer_code_error(
        selected_material.code
        if selected_material is not None
        else (payload.material or item.snapshot_material),
        prospective_item_layer,
    )
    if material_code_error:
        raise HTTPException(status_code=400, detail=f"订单明细{material_code_error}")
    prospective_item_flute = _validated_order_layer_flute(
        prospective_item_layer,
        payload.flute_type if payload.flute_type is not None else item.flute_type,
        detail_prefix="订单明细",
    )
    product_to_sync: Product | None = None
    prospective_product_layer: int | None = None
    prospective_product_flute: str | None = None
    if payload.sync_product and item.product_id:
        product_to_sync = db.get(Product, item.product_id)
        if product_to_sync is None:
            raise HTTPException(status_code=409, detail="关联常用箱不存在，订单明细未保存")
        product_material_id = (
            payload.material_id
            if payload.material_id is not None
            else product_to_sync.material_id
        )
        product_material = (
            db.get(Material, product_material_id)
            if product_material_id is not None
            else None
        )
        if product_material_id is not None and product_material is None:
            raise HTTPException(status_code=400, detail="关联常用箱材质不存在")
        if (
            product_material is not None
            and payload.layer_count is not None
            and payload.layer_count != product_material.layer_count
        ):
            raise HTTPException(status_code=400, detail="关联常用箱请求层数与所选材质真实层数不一致")
        prospective_product_layer = (
            product_material.layer_count
            if product_material is not None
            else (
                payload.layer_count
                if payload.layer_count is not None
                else product_to_sync.layer_count
            )
        )
        prospective_product_flute = _validated_order_layer_flute(
            prospective_product_layer,
            (
                payload.flute_type
                if payload.flute_type is not None
                else product_to_sync.flute_type
            ),
            detail_prefix="关联常用箱",
        )
    report_field_mapping = {
        "snapshot_report_length_mm": "report_length_mm",
        "snapshot_report_width_mm": "report_width_mm",
        "snapshot_crease_type": "crease_type",
        "snapshot_crease_left_mm": "crease_left_mm",
        "snapshot_crease_middle_mm": "crease_middle_mm",
        "snapshot_crease_right_mm": "crease_right_mm",
        "snapshot_report_notes": "report_notes",
        "snapshot_base_report_length_mm": "base_report_length_mm",
        "snapshot_base_report_width_mm": "base_report_width_mm",
        "snapshot_base_crease_type": "base_crease_type",
        "snapshot_base_crease_left_mm": "base_crease_left_mm",
        "snapshot_base_crease_middle_mm": "base_crease_middle_mm",
        "snapshot_base_crease_right_mm": "base_crease_right_mm",
        "snapshot_base_report_notes": "base_report_notes",
    }

    def normalized_report_value(field_name: str, value: object) -> object:
        if field_name in {
            "snapshot_crease_type",
            "snapshot_base_crease_type",
            "snapshot_report_notes",
            "snapshot_base_report_notes",
        }:
            return value or None
        return value

    changed_report_fields: set[str] = set()
    for field_name in report_field_mapping:
        value = getattr(payload, field_name)
        if value is None:
            continue
        normalized = normalized_report_value(field_name, value)
        if normalized != getattr(item, field_name):
            changed_report_fields.add(field_name)

    main_validation_fields = {
        "snapshot_report_length_mm",
        "snapshot_report_width_mm",
        "snapshot_crease_type",
        "snapshot_crease_left_mm",
        "snapshot_crease_middle_mm",
        "snapshot_crease_right_mm",
    }
    if main_validation_fields.intersection(changed_report_fields):
        error = crease_width_error(
            label="压线",
            crease_type=normalized_report_value(
                "snapshot_crease_type",
                payload.snapshot_crease_type
                if payload.snapshot_crease_type is not None
                else item.snapshot_crease_type,
            ),
            report_width_mm=(
                payload.snapshot_report_width_mm
                if payload.snapshot_report_width_mm is not None
                else item.snapshot_report_width_mm
            ),
            left_mm=(
                payload.snapshot_crease_left_mm
                if payload.snapshot_crease_left_mm is not None
                else item.snapshot_crease_left_mm
            ),
            middle_mm=(
                payload.snapshot_crease_middle_mm
                if payload.snapshot_crease_middle_mm is not None
                else item.snapshot_crease_middle_mm
            ),
            right_mm=(
                payload.snapshot_crease_right_mm
                if payload.snapshot_crease_right_mm is not None
                else item.snapshot_crease_right_mm
            ),
        )
        if error:
            raise HTTPException(status_code=400, detail=error)

    base_validation_fields = {
        "snapshot_base_report_length_mm",
        "snapshot_base_report_width_mm",
        "snapshot_base_crease_type",
        "snapshot_base_crease_left_mm",
        "snapshot_base_crease_middle_mm",
        "snapshot_base_crease_right_mm",
    }
    if base_validation_fields.intersection(changed_report_fields):
        error = crease_width_error(
            label="底压线",
            crease_type=normalized_report_value(
                "snapshot_base_crease_type",
                payload.snapshot_base_crease_type
                if payload.snapshot_base_crease_type is not None
                else item.snapshot_base_crease_type,
            ),
            report_width_mm=(
                payload.snapshot_base_report_width_mm
                if payload.snapshot_base_report_width_mm is not None
                else item.snapshot_base_report_width_mm
            ),
            left_mm=(
                payload.snapshot_base_crease_left_mm
                if payload.snapshot_base_crease_left_mm is not None
                else item.snapshot_base_crease_left_mm
            ),
            middle_mm=(
                payload.snapshot_base_crease_middle_mm
                if payload.snapshot_base_crease_middle_mm is not None
                else item.snapshot_base_crease_middle_mm
            ),
            right_mm=(
                payload.snapshot_base_crease_right_mm
                if payload.snapshot_base_crease_right_mm is not None
                else item.snapshot_base_crease_right_mm
            ),
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
    before = {
        "quantity": item.quantity,
        "unit_price": str(item.unit_price),
        "product_code": item.snapshot_product_code,
        "product_name": item.snapshot_product_name,
        "material": item.snapshot_material,
        "specification": item.snapshot_spec,
    }
    quantity_delta = int(payload.quantity) - int(item.quantity or 0)
    if quantity_delta and is_composite_order_item(db, item.id):
        adjustment_count = int(
            db.scalar(
                select(func.count(SalesOrderItemBomDemandAdjustment.id))
                .join(
                    SalesOrderItemBomComponent,
                    SalesOrderItemBomComponent.id
                    == SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id,
                )
                .where(SalesOrderItemBomComponent.sales_order_item_id == item.id)
            )
            or 0
        )
        adjustment_key = (
            (payload.quantity_adjustment_idempotency_key or "").strip()
            or (
                f"order-item-{item.id}-quantity-adjustment-{adjustment_count + 1}-"
                f"{item.quantity}-to-{payload.quantity}"
            )
        )
        try:
            append_order_quantity_adjustments(
                db,
                order_item_id=item.id,
                delta_sets=quantity_delta,
                reason=(
                    (payload.quantity_adjustment_reason or "").strip()
                    or "订单明细数量修改"
                ),
                actor_id=user.id,
                idempotency_key=adjustment_key,
            )
        except CompositeBomWorkflowError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
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
        item.material_id = selected_material_id
        item.snapshot_supplier_name = selected_material.supplier_name
        item.snapshot_weight = selected_material.basis_weight_description
    item.layer_count = prospective_item_layer
    item.flute_type = prospective_item_flute
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
    if product_to_sync is not None:
        product = product_to_sync
        product_updates: dict[str, object] = {}

        def add_product_update(field_name: str, value: object) -> None:
            if getattr(product, field_name) != value:
                product_updates[field_name] = value

        if payload.material_id is not None:
            add_product_update("material_id", selected_material_id)
        add_product_update("layer_count", prospective_product_layer)
        add_product_update("flute_type", prospective_product_flute)
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
                add_product_update(field_name, value)
        splice_mode = payload.snapshot_splice_mode or product.splice_mode or "single"
        add_product_update("splice_mode", splice_mode)
        pieces_per_box = (
            payload.snapshot_pieces_per_box
            if payload.snapshot_pieces_per_box is not None
            else (2 if splice_mode == "double" else 1)
        )
        add_product_update("pieces_per_box", pieces_per_box)
        if payload.snapshot_flap_mm is not None:
            add_product_update("flap_mm", payload.snapshot_flap_mm)
        for snapshot_field, product_field in report_field_mapping.items():
            if snapshot_field not in changed_report_fields:
                continue
            value = normalized_report_value(
                snapshot_field,
                getattr(payload, snapshot_field),
            )
            add_product_update(product_field, value)
        if payload.product_remark is not None:
            add_product_update("remark", payload.product_remark.strip() or None)

        from app.services.master_data_versioning import apply_versioned_update

        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=product_updates,
            expected_version=payload.product_expected_version,
            user=user,
            reason=product_change_reason,
            source="orders.update-item.sync-product",
            confirmation_token=payload.product_confirmation_token,
        )
    db.flush()
    try:
        if is_composite_order_item(db, item.id):
            refresh_production_task(db, item.id, create_if_missing=True)
        elif refresh_production_task(db, item.id) is not None:
            refresh_order_production_status(db, item.order_id)
    except ProductionWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
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
        "snapshot_original_material_code": item.snapshot_original_material_code,
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
    user: User = Depends(can_delete),
) -> Response:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    _lock_orders_for_production_transition(db, [order.id])
    item = db.scalar(
        select(OrderItem)
        .where(OrderItem.id == item_id)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise HTTPException(status_code=409, detail="订单明细已被删除，请刷新后重试")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=409, detail="订单已被删除，请刷新后重试")
    _ensure_no_production_completion_facts(db, [item.id])
    if item.delivered_quantity > 0 or item.material_status == "received":
        raise HTTPException(status_code=409, detail="已流转明细禁止删除")
    if item.requisition_status != "未报料":
        raise HTTPException(status_code=409, detail="请先取消报料再删除订单明细")
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
