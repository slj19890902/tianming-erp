from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from datetime import date, datetime
from decimal import Decimal
from threading import Lock
from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, load_only, selectinload

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.models.historical_purchase import HistoricalPurchaseEntry
from app.models.incoming_receipt import IncomingReceiptItem
from app.core.time_contract import (
    beijing_naive_to_api,
    beijing_now_naive,
    beijing_today,
    utc_naive_to_api,
    utc_naive_to_beijing_date,
    utc_now_naive,
)
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.customer_material import (
    CustomerMaterialCandidate,
    CustomerMaterialSelectionHistory,
)
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion, ProductionTask
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.requisition import Requisition, RequisitionHold, RequisitionItem
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.stock_replenishment import (
    InventoryStockPolicy,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WarehouseLocation,
)
from app.services.history_orders import (
    build_display_registry,
    build_display_registry_for_order_ids,
    display_order_number,
    is_history_order_number,
)
from app.services.audit_log import append_audit_event
from app.services.historical_purchase_lookup import (
    DEFAULT_SHEET_NAME,
    _historical_purchase_display_key,
    _historical_purchase_group_response,
    normalize_lookup_text,
    search_historical_purchase_database,
)
from app.services.location_candidates import (
    list_operational_locations,
    operational_location_issue,
)
from app.services.audit_log import append_audit_event
from app.services.stock_replenishment import (
    StockReplenishmentError,
    finished_product_quantity_summary,
    next_replenishment_order_number,
    product_replenishment_defaults,
    product_replenishment_signature,
    replenishment_order_dict,
    stock_policy_dict,
    stock_replenishment_order,
    theoretical_requisition_quantity,
    validate_stock_policy,
)
from app.services.supplier_master import (
    SupplierLookupError,
    normalize_supplier_identity,
    resolve_supplier,
)
from app.services.flute_mapping import (
    normalize_flute_type,
    seven_layer_code_error,
    validate_flute_for_write,
)
from app.services.report_crease import crease_width_error
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    component_inventory_coverage,
    finished_inventory_candidates,
    has_unconsumed_inventory_reservations,
    inventory_fifo_sort_key,
    normalize_material_code,
    requisition_finished_inventory_coverage_by_item_ids,
    requisition_finished_inventory_coverage_qty,
    release_active_finished_reservations_for_items,
    reserve_finished_inventory,
)
from app.services.semi_finished_inventory import (
    SIGNATURE_OVERRIDE_WARNING,
    SemiFinishedCandidate,
    SemiFinishedLotVersion,
    active_semi_reserved_piece_qty,
    browse_semi_finished_inventory_for_product,
    ensure_semi_finished_lot_eligibility,
    release_active_semi_reservations_for_items,
    requirement_signature,
    reserve_semi_finished_inventory,
    safe_physical_board_facts_match,
    save_order_item_semi_requirement,
    semi_finished_candidates_for_product,
    semi_finished_inventory_candidates,
)
from app.services.composite_bom_execution import (
    CompositeBOMExecutionError,
    require_positive_integer,
)
from app.services.composite_bom_workflow import effective_component_demands
from app.services.box_type_rules import (
    BoxTypeRuleError,
    recommend_box_type,
)
from app.services.customer_material_candidates import (
    candidate_response,
    normalize_material_candidate_key,
    normalize_supplier_candidate_key,
)
from app.services import material_pricing
from app.services.requisition_quantities import (
    CUTTING_MODE_BOX_STYLES,
    DEFAULT_CUTTING_MODE,
    CuttingModeError,
    cutting_factor,
    normalize_cutting_mode,
    purchase_sheet_quantity,
    required_piece_quantity,
)
from app.services.requisition_production_print import (
    build_supplier_requisition_production_package,
)
from app.services.requisition_production_print_batch import (
    ProductionPrintBatchError,
    build_selected_production_print_package,
    canonical_batch_items,
    production_print_batch_guard,
    production_print_batch_id,
    production_print_batch_request_hash,
)
from app.services.production_packaging_label import (
    build_supplier_requisition_packaging_label_package,
)
from app.services.production_packaging_label_layout import (
    ProductionPackagingLabelLayoutConflict,
    ProductionPackagingLabelLayoutError,
    admin_state as production_packaging_label_layout_admin_state,
    effective_layout as effective_production_packaging_label_layout,
    layout_diff_summary,
    publish_draft as publish_production_packaging_label_layout_draft,
    restore_default as restore_default_production_packaging_label_layout,
    rollback_release as rollback_production_packaging_label_layout,
    save_draft as save_production_packaging_label_layout_draft,
)
from app.services.production_label_operations import (
    ProductionLabelOperationError,
    confirm_packaging_label_job_printed,
    get_packaging_label_job,
    latest_printed_job_metadata,
    packaging_label_job_response,
    prepare_packaging_label_job,
    production_label_write_guard,
)


router = APIRouter()
can_read = PermissionChecker("requisition.view")
can_operate = PermissionChecker("requisition.execute")
can_reserve = PermissionChecker("warehouse.reserve")
can_read_production_labels = PermissionChecker("orders.view")
admin_rollback = RoleChecker(["admin"])
admin_production_label_layout = RoleChecker(["admin"])
_FINISHED_STOCK_POLICY_WRITE_LOCK = Lock()
_SUPPLIER_ORDER_CREATE_WRITE_LOCK = Lock()
_SUPPLIER_ORDER_ITEM_VOID_WRITE_LOCK = Lock()


class ProductionPackagingLabelJobRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    confirmed: Literal[True]

    @field_validator("idempotency_key", "plan_fingerprint")
    @classmethod
    def trim_label_job_values(cls, value: str) -> str:
        return value.strip()


class ProductionPackagingLabelPrintConfirmationRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def trim_label_confirmation_key(cls, value: str) -> str:
        return value.strip()


class ProductionPackagingLabelLayoutPaperPayload(BaseModel):
    width_mm: float
    height_mm: float


class ProductionPackagingLabelLayoutElementPayload(BaseModel):
    id: str = Field(min_length=1, max_length=50)
    kind: Literal["text", "qr"]
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    font_size_mm: float | None = None
    font_weight: int | None = None
    text_align: Literal["left", "center", "right"] | None = None
    visible: bool


class ProductionPackagingLabelLayoutPayload(BaseModel):
    catalog_version: str = Field(min_length=1, max_length=40)
    paper: ProductionPackagingLabelLayoutPaperPayload
    elements: list[ProductionPackagingLabelLayoutElementPayload]


class ProductionPackagingLabelLayoutDraftRequest(BaseModel):
    operation_key: str = Field(min_length=1, max_length=120)
    expected_draft_version: int = Field(ge=0)
    layout: ProductionPackagingLabelLayoutPayload

    @field_validator("operation_key")
    @classmethod
    def trim_layout_operation_key(cls, value: str) -> str:
        return value.strip()


class ProductionPackagingLabelLayoutReleaseRequest(BaseModel):
    operation_key: str = Field(min_length=1, max_length=120)
    expected_draft_version: int = Field(ge=0)
    expected_release_version: int = Field(ge=0)

    @field_validator("operation_key")
    @classmethod
    def trim_layout_release_operation_key(cls, value: str) -> str:
        return value.strip()


class ProductionPrintTaskVersion(BaseModel):
    task_id: int = Field(gt=0)
    version: int = Field(gt=0)


class ProductionPrintBatchItem(BaseModel):
    supplier_order_id: int = Field(gt=0)
    source_identity: str = Field(min_length=1, max_length=160)
    selection_fingerprint: str = Field(min_length=64, max_length=64)
    task_versions: list[ProductionPrintTaskVersion] = Field(min_length=1, max_length=6)

    @field_validator("source_identity", "selection_fingerprint")
    @classmethod
    def trim_production_print_item_values(cls, value: str) -> str:
        return value.strip()


class ProductionPrintBatchRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    confirmed: Literal[True]
    items: list[ProductionPrintBatchItem] = Field(min_length=1, max_length=20)

    @field_validator("idempotency_key")
    @classmethod
    def trim_production_print_batch_key(cls, value: str) -> str:
        return value.strip()


def _require_active_supplier(
    db: Session,
    supplier_name: object,
    *,
    detail_prefix: str = "",
) -> str:
    try:
        supplier = resolve_supplier(db, supplier_name, require_active=True)
    except SupplierLookupError as error:
        raise HTTPException(
            status_code=400,
            detail=f"{detail_prefix}{error.message}",
        ) from error
    return supplier.standard_name


def _require_active_material_supplier(
    db: Session,
    material: Material | None,
    *,
    detail_prefix: str = "",
) -> None:
    if material is not None and (material.supplier_name or "").strip():
        _require_active_supplier(
            db,
            material.supplier_name,
            detail_prefix=detail_prefix,
        )


INACTIVE_REQUISITION_ITEM_STATUSES = {
    "cancelled",
    "canceled",
    "voided",
    "withdrawn",
    "invalid",
    "已取消",
    "已作废",
    "已撤回",
}
NON_EFFECTIVE_LEGACY_REQUISITION_STATUSES = (
    INACTIVE_REQUISITION_ITEM_STATUSES
    | {
        "merged_pending",
        "supplier_requisition_created",
    }
)
SPECIAL_PROCESSES = {"无", "大做小", "双拼", "多拼"}
SUPPLIER_MATERIAL_FLUTES = {"AAA", "ABC", "AB", "E", "BE", "B", "C", "A"}


def _validated_cutting_mode(value: object) -> str:
    try:
        return normalize_cutting_mode(value, strict=True)
    except CuttingModeError as error:
        raise ValueError(str(error)) from error


def _business_flute_error(
    layer_count: int | None,
    flute_type: str | None,
) -> tuple[str | None, str | None]:
    """Return the normalized business flute and its write-time validation error."""
    normalized_flute = normalize_flute_type(flute_type)
    error = validate_flute_for_write(normalized_flute, layer_count)
    return normalized_flute, error


def _clean_supplier_material_code(value: str | None, layer_count: int | None) -> str:
    """清洗供应商报料材质代码，去掉楞型和历史组合尾巴。"""
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    expected_length = {3: 3, 5: 5, 7: 7}.get(layer_count)
    # 供应商材质代码可以包含 + 等实际符号。优先按完整可见代码读取，
    # 避免旧的字母数字 token 提取把 A+A 错拆成单个 A。
    if expected_length:
        for candidate in re.split(r"\s*/\s*|\s*\|\s*", raw):
            compact = re.sub(r"\s+", "", candidate)
            if (
                len(compact) == expected_length
                and all(char.isprintable() and not char.isspace() for char in compact)
            ):
                return compact
    tokens = re.findall(r"[A-Z0-9]+", raw)
    candidates = [token for token in tokens if any(char.isalpha() for char in token)]
    if not candidates:
        return ""
    # AB/BE、B/E 等只有楞型的组合不是材质代码。
    if len(candidates) > 1 and all(token in SUPPLIER_MATERIAL_FLUTES for token in candidates):
        return ""
    code = candidates[0]
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
    bom_snapshot_id: int | None = Field(default=None, gt=0)
    actual_yield_per_sheet: int | None = Field(default=None, gt=0, strict=True)
    inventory_deducted_qty: int = Field(default=0, ge=0)
    requisition_qty: int | None = Field(default=None, ge=0)
    cardboard_len: Decimal = Field(gt=0)
    cardboard_width: Decimal = Field(gt=0)
    special_process: str = DEFAULT_CUTTING_MODE
    remark: str | None = None

    @field_validator("special_process")
    @classmethod
    def validate_process(cls, value: str) -> str:
        return _validated_cutting_mode(value)

    @field_validator("component_type")
    @classmethod
    def validate_component_type(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"whole", "cover", "base"}:
            raise ValueError("组件类型仅允许 whole、cover 或 base")
        return normalized

    @model_validator(mode="after")
    def validate_bom_component_selection(self):
        return self


class PendingMaterialUpdate(BaseModel):
    material_id: int
    layer_count: int | None = None
    flute_type: str | None = None
    sync_product: bool = False
    product_expected_version: int | None = Field(default=None, ge=1)
    product_change_reason: str | None = Field(default=None, max_length=500)
    product_confirmation_token: str | None = Field(default=None, max_length=2000)
    candidate_id: int | None = Field(default=None, gt=0)
    source_type: str | None = Field(default=None, max_length=50)
    source_reference: str | None = Field(default=None, max_length=250)
    selection_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("flute_type")
    @classmethod
    def normalize_flute(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().upper()
        return normalized or None


class PendingVirtualCompositeParentUpdate(BaseModel):
    product_expected_version: int = Field(ge=1)
    product_confirmation_token: str | None = Field(default=None, max_length=2000)
    confirmed: Literal[True]


class CustomerMaterialCandidateCreate(BaseModel):
    customer_id: int = Field(gt=0)
    original_material_code: str = Field(min_length=1, max_length=250)
    material_id: int | None = Field(default=None, gt=0)
    actual_material_id: int | None = Field(default=None, gt=0)
    supplier_name: str | None = Field(default=None, max_length=200)
    manual_priority: int = Field(default=0, ge=0, le=10000)
    is_active: bool = True
    source: str = Field(default="manual", min_length=1, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def normalize_material_id(self):
        if self.material_id is not None and self.actual_material_id is not None:
            if self.material_id != self.actual_material_id:
                raise ValueError("material_id 与 actual_material_id 不能指向不同材质")
        self.material_id = self.material_id or self.actual_material_id
        if self.material_id is None:
            raise ValueError("必须选择实际报料材质")
        return self


class CustomerMaterialCandidateUpdate(BaseModel):
    original_material_code: str | None = Field(default=None, min_length=1, max_length=250)
    material_id: int | None = Field(default=None, gt=0)
    actual_material_id: int | None = Field(default=None, gt=0)
    supplier_name: str | None = Field(default=None, max_length=200)
    manual_priority: int | None = Field(default=None, ge=0, le=10000)
    is_active: bool | None = None
    source: str | None = Field(default=None, min_length=1, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def normalize_material_id(self):
        if self.material_id is not None and self.actual_material_id is not None:
            if self.material_id != self.actual_material_id:
                raise ValueError("material_id 与 actual_material_id 不能指向不同材质")
        if self.material_id is None and self.actual_material_id is not None:
            self.material_id = self.actual_material_id
            self.__pydantic_fields_set__.add("material_id")
        return self


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
        seen_bom_sources: set[tuple[int, str]] = set()
        for item in value:
            if item.bom_snapshot_id is not None:
                source_key = (
                    item.bom_snapshot_id,
                    item.component_type or "whole",
                )
                if source_key in seen_bom_sources:
                    raise ValueError("同一复合产品物理料不能重复报料")
                seen_bom_sources.add(source_key)
                continue
            if item.component_type:
                key = (item.order_item_id, item.component_type)
                if key in seen_components:
                    raise ValueError("同一天地盖组件不能重复报料")
                seen_components.add(key)
                continue
            if item.order_item_id in seen_single:
                raise ValueError("同一订单明细不能重复报料")
            seen_single.add(item.order_item_id)
        component_order_item_ids = {
            order_item_id for order_item_id, _ in seen_components
        }
        bom_order_item_ids = {
            item.order_item_id for item in value if item.bom_snapshot_id is not None
        }
        if seen_single & component_order_item_ids:
            raise ValueError("同一订单明细不能同时按整单和天地盖组件报料")
        if component_order_item_ids & bom_order_item_ids:
            raise ValueError("同一订单明细不能同时按天地盖和复合产品组件报料")
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
        return _validated_cutting_mode(value)


class SupplierSchedulePayload(BaseModel):
    supplier_delivery_time: datetime
    supplier_order_number: str | None = None


class CancelPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class SupplierRequisitionItemVoidPayload(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def trim_idempotency_key(cls, value: str) -> str:
        return value.strip()


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
        return _validated_cutting_mode(value)


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
        return _validated_cutting_mode(value)


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
        return _validated_cutting_mode(value)


class PendingSupplierOrderCreatePayload(BaseModel):
    selections: list[PendingSupplierOrderSelection] = Field(min_length=1)


class RequisitionHoldSelection(BaseModel):
    order_item_id: int = Field(gt=0)
    release_mode: str
    previous_order_item_id: int | None = Field(default=None, gt=0)
    expected_requisition_date: date | None = None

    @field_validator("release_mode")
    @classmethod
    def validate_release_mode(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if normalized not in {"previous_batch_completed", "expected_date"}:
            raise ValueError("等候条件仅允许等上一批送完或指定日期")
        return normalized

    @model_validator(mode="after")
    def validate_release_condition(self):
        if self.release_mode == "previous_batch_completed":
            if self.previous_order_item_id is None:
                raise ValueError("请选择要等待的上一批")
            if self.expected_requisition_date is not None:
                raise ValueError("等上一批送完时不能再填写指定日期")
        elif self.expected_requisition_date is None:
            raise ValueError("请选择预计恢复报料日期")
        elif self.previous_order_item_id is not None:
            raise ValueError("指定日期时不能再选择上一批")
        return self


class RequisitionHoldBatchCreatePayload(BaseModel):
    items: list[RequisitionHoldSelection] = Field(min_length=1, max_length=100)


class RequisitionHoldUpdatePayload(BaseModel):
    expected_version: int = Field(gt=0)
    release_mode: str
    previous_order_item_id: int | None = Field(default=None, gt=0)
    expected_requisition_date: date | None = None

    @field_validator("release_mode")
    @classmethod
    def validate_release_mode(cls, value: str) -> str:
        return RequisitionHoldSelection.validate_release_mode(value)

    @model_validator(mode="after")
    def validate_release_condition(self):
        RequisitionHoldSelection(
            order_item_id=1,
            release_mode=self.release_mode,
            previous_order_item_id=self.previous_order_item_id,
            expected_requisition_date=self.expected_requisition_date,
        )
        return self


class RequisitionHoldReleasePayload(BaseModel):
    expected_version: int = Field(gt=0)


class OrderEntryHoldPreviewLine(BaseModel):
    client_line_id: str = Field(min_length=1, max_length=80)
    product_code: str | None = Field(default=None, max_length=150)
    specification: str | None = Field(default=None, max_length=150)
    material: str | None = Field(default=None, max_length=150)
    flute_type: str | None = Field(default=None, max_length=30)
    quantity: int = Field(gt=0)
    finished_covered_quantity: int = Field(default=0, ge=0)


class OrderEntryHoldPreviewPayload(BaseModel):
    customer_id: int = Field(gt=0)
    lines: list[OrderEntryHoldPreviewLine] = Field(min_length=1, max_length=100)


class PendingSemiInventoryLot(BaseModel):
    lot_id: int
    expected_version: int = Field(gt=0)


class PendingSemiInventoryReservationPayload(BaseModel):
    order_item_id: int
    component_type: str
    requested_requirement_quantity: int = Field(gt=0)
    lots: list[PendingSemiInventoryLot] = Field(min_length=1)
    override: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=80)

    @field_validator("component_type")
    @classmethod
    def validate_component_type(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"whole", "cover", "base"}:
            raise ValueError("半成品组件仅允许 whole、cover 或 base")
        return normalized


class PendingCustomerBoardPreparationAutoCoverPayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=60)


class PendingLateFinishedInventoryAutoReservePayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=60)


class StockPolicyPayload(BaseModel):
    policy_name: str = Field(min_length=1, max_length=200)
    target_inventory_type: str
    product_id: int | None = None
    customer_id: int | None = None
    material_code: str | None = Field(default=None, max_length=100)
    layer_count: int | None = None
    flute_type: str | None = Field(default=None, max_length=20)
    report_length_mm: int | None = Field(default=None, gt=0)
    report_width_mm: int | None = Field(default=None, gt=0)
    sheet_type: str = "raw_board"
    component_type: str = "whole"
    pieces_per_box: int = Field(default=1, gt=0)
    stock_yield_per_sheet: int = Field(default=1, gt=0)
    warning_quantity: int = Field(ge=0)
    target_quantity: int = Field(gt=0)
    default_location_id: int | None = None
    supplier_name: str | None = Field(default=None, max_length=200)
    remark: str | None = None
    active: bool = True

    @field_validator("target_inventory_type")
    @classmethod
    def valid_target_type(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"finished", "semi_finished"}:
            raise ValueError("库存目标类型仅允许 finished 或 semi_finished")
        return normalized

    @field_validator("flute_type")
    @classmethod
    def normalize_flute_type(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().upper()
        return normalized or None

    @field_validator("sheet_type")
    @classmethod
    def valid_sheet_type(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"raw_board", "net_sheet", "creased_sheet"}:
            raise ValueError("半成品片料类型无效")
        return normalized

    @field_validator("component_type")
    @classmethod
    def valid_stock_component(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"whole", "cover", "base"}:
            raise ValueError("半成品组件仅允许 whole、cover 或 base")
        return normalized


class FinishedStockPolicyQuickPayload(BaseModel):
    warning_quantity: int = Field(ge=0)
    target_quantity: int = Field(gt=0)


class StockReplenishmentItemPayload(BaseModel):
    stock_policy_id: int | None = None
    target_inventory_type: str
    product_id: int | None = None
    customer_id: int | None = None
    material_id: int | None = None
    product_code: str | None = Field(default=None, max_length=150)
    product_name: str | None = Field(default=None, max_length=250)
    material_code: str | None = Field(default=None, max_length=100)
    layer_count: int | None = None
    flute_type: str | None = Field(default=None, max_length=20)
    report_length_mm: int | None = Field(default=None, gt=0)
    report_width_mm: int | None = Field(default=None, gt=0)
    crease_type: str | None = Field(default=None, max_length=20)
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    sheet_type: str = "raw_board"
    component_type: str = "whole"
    pieces_per_box: int = Field(default=1, gt=0)
    stock_yield_per_sheet: int = Field(default=1, gt=0)
    quantity: int = Field(gt=0)
    location_id: int | None = None
    historical_workbook: str | None = Field(default=None, max_length=260)
    historical_sheet: str | None = Field(default=None, max_length=150)
    historical_row: int | None = Field(default=None, gt=0)
    historical_search_text: str | None = None
    remark: str | None = None

    @field_validator("target_inventory_type")
    @classmethod
    def valid_item_target_type(cls, value: str) -> str:
        return StockPolicyPayload.valid_target_type(value)

    @field_validator("flute_type")
    @classmethod
    def normalize_item_flute(cls, value: str | None) -> str | None:
        return StockPolicyPayload.normalize_flute_type(value)

    @field_validator("sheet_type")
    @classmethod
    def valid_item_sheet_type(cls, value: str) -> str:
        return StockPolicyPayload.valid_sheet_type(value)

    @field_validator("component_type")
    @classmethod
    def valid_item_component(cls, value: str) -> str:
        return StockPolicyPayload.valid_stock_component(value)

    @model_validator(mode="after")
    def normalize_crease_fields(self) -> "StockReplenishmentItemPayload":
        aliases = {"毛": "毛片", "净": "净料"}
        crease_type = aliases.get(str(self.crease_type or "").strip(), str(self.crease_type or "").strip())
        self.crease_type = crease_type or None
        if self.crease_type == "压线":
            segments = (
                self.crease_left_mm,
                self.crease_middle_mm,
                self.crease_right_mm,
            )
            if not all(value is not None for value in segments):
                raise ValueError("压线类型必须完整填写上摇盖、高、下摇盖")
            self.sheet_type = "creased_sheet"
        elif self.crease_type in {"毛片", "净料", "其他"}:
            self.crease_left_mm = None
            self.crease_middle_mm = None
            self.crease_right_mm = None
            if self.crease_type == "净料":
                self.sheet_type = "net_sheet"
            elif self.crease_type == "毛片":
                self.sheet_type = "raw_board"
        return self


class StockReplenishmentCreatePayload(BaseModel):
    source_type: str = "manual_history"
    idempotency_key: str | None = Field(default=None, max_length=80)
    supplier_name: str | None = Field(default=None, max_length=200)
    customer_id: int | None = None
    remark: str | None = None
    stock_now: bool = False
    items: list[StockReplenishmentItemPayload] = Field(min_length=1)

    @field_validator("source_type")
    @classmethod
    def valid_source_type(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"stock_warning", "customer_request", "manual_history"}:
            raise ValueError("补库来源类型无效")
        return normalized

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


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
        return _validated_cutting_mode(value)


class PendingSupplierOrderDraftSourceItem(BaseModel):
    source_type: str
    order_item_id: int
    merge_group_id: int | None = None
    component_type: str | None = None
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

    @field_validator("component_type")
    @classmethod
    def validate_component_type(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"whole", "cover", "base"}:
            raise ValueError("采购草稿组件仅允许 whole、cover 或 base")
        return normalized


class PendingSupplierOrderDraftLine(BaseModel):
    line_key: str | None = None
    source_type: str | None = None
    report_length_mm: Decimal = Field(gt=0)
    report_width_mm: Decimal = Field(gt=0)
    cutting_mode: str = DEFAULT_CUTTING_MODE
    inventory_deducted_qty: int = 0
    requisition_qty: int
    dimension_override_acknowledged: bool = False
    quantity_override_acknowledged: bool = False
    remark: str | None = None
    source_items: list[PendingSupplierOrderDraftSourceItem] = Field(min_length=1)

    @field_validator("cutting_mode")
    @classmethod
    def validate_cutting_mode(cls, value: str) -> str:
        return _validated_cutting_mode(value)


class PendingSupplierOrderDraftGroup(BaseModel):
    supplier_name: str | None = None
    request_key: str | None = None
    lines: list[PendingSupplierOrderDraftLine] = Field(default_factory=list)
    # Backward-compatible input for the previous flat source-item draft shape.
    items: list[dict] = Field(default_factory=list)

    @field_validator("request_key")
    @classmethod
    def validate_request_key(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", normalized):
            raise ValueError("报料请求编号格式不正确，请刷新草稿后重试")
        return normalized


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


def _bom_snapshot_component_types(
    snapshot: SalesOrderItemBomComponent,
) -> tuple[str, ...]:
    if _is_telescoping_lid_box(snapshot.snapshot_component_box_style):
        return ("cover", "base")
    return ("whole",)


def _bom_snapshot_component_type(
    snapshot: SalesOrderItemBomComponent,
    component_type: str | None,
) -> str:
    requested = (component_type or "").strip().lower()
    allowed = _bom_snapshot_component_types(snapshot)
    if not requested:
        if allowed == ("whole",):
            return "whole"
        raise HTTPException(
            status_code=400,
            detail="组合 A3 天地盖必须明确选择盖片或底片",
        )
    if requested not in allowed:
        labels = "盖片或底片" if allowed != ("whole",) else "整片"
        raise HTTPException(
            status_code=400,
            detail=f"该组合组件只允许选择{labels}",
        )
    return requested


def _bom_snapshot_physical_pieces_per_component(
    snapshot: SalesOrderItemBomComponent,
    component_type: str,
) -> int:
    # An A3 component product always contributes one cover and one base.  The
    # snapshot's splice value describes an ordinary whole component such as a
    # two-piece surround panel; it must not double A3 cover/base again.
    if component_type in {"cover", "base"}:
        return 1
    frozen_value = int(snapshot.snapshot_component_pieces_per_box or 0)
    if frozen_value > 0:
        return frozen_value
    if (snapshot.snapshot_component_splice_mode or "").strip().lower() == "double":
        return 2
    return 1


def _is_surround_panel_snapshot(
    snapshot: SalesOrderItemBomComponent,
) -> bool:
    box_style = (snapshot.snapshot_component_box_style or "").strip()
    product_name = (snapshot.snapshot_component_product_name or "").strip()
    return box_style in {"围板", "围套"} or "围板" in product_name


def _is_set_only_a3_surround_bom(
    snapshots: list[SalesOrderItemBomComponent],
) -> bool:
    if len(snapshots) != 2:
        return False
    try:
        if any(int(snapshot.quantity_per_set) != 1 for snapshot in snapshots):
            return False
    except (TypeError, ValueError):
        return False
    return (
        sum(
            1
            for snapshot in snapshots
            if _is_telescoping_lid_box(snapshot.snapshot_component_box_style)
        )
        == 1
        and sum(1 for snapshot in snapshots if _is_surround_panel_snapshot(snapshot))
        == 1
    )


def _composite_parent_requisition_is_suppressed(
    item: OrderItem,
    snapshots: list[SalesOrderItemBomComponent],
) -> bool:
    """Return whether the parent is a commercial set identity, not a board source.

    This predicate is shared by pending-list projection, completion checks and
    the formal requisition write path.  Keeping those paths on one rule avoids
    showing component-only demand in the UI and then requiring a hidden parent
    board again when the operator saves the reviewed draft.
    """

    return bool(
        getattr(item, "is_virtual_composite_parent_snapshot", False)
    ) or _is_set_only_a3_surround_bom(snapshots)


def _requisition_item_component(item: RequisitionItem | None) -> str:
    name = (item.product_name_snapshot if item is not None else "") or ""
    if name.endswith("-底"):
        return "base"
    if name.endswith("-盖"):
        return "cover"
    return "whole"


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


def _bom_snapshot_crease(
    snapshot: SalesOrderItemBomComponent,
    component: str | None,
) -> tuple[str | None, int | None, int | None, int | None]:
    """Return immutable physical-board crease facts for one BOM source."""
    if component == "base":
        base_values = (
            snapshot.snapshot_component_base_crease_type,
            snapshot.snapshot_component_base_crease_left_mm,
            snapshot.snapshot_component_base_crease_middle_mm,
            snapshot.snapshot_component_base_crease_right_mm,
        )
        if any(value is not None for value in base_values):
            return base_values
    return (
        snapshot.snapshot_component_crease_type,
        snapshot.snapshot_component_crease_left_mm,
        snapshot.snapshot_component_crease_middle_mm,
        snapshot.snapshot_component_crease_right_mm,
    )


def _bom_snapshot_report_notes(
    snapshot: SalesOrderItemBomComponent,
    component: str | None,
) -> str | None:
    return (
        snapshot.snapshot_component_base_report_notes
        if component == "base"
        else snapshot.snapshot_component_report_notes
    )


def _cutting_factor(cutting_mode: str | None) -> int:
    return cutting_factor(cutting_mode)


def _required_piece_qty(order_qty: int, pieces_per_box: int) -> int:
    return required_piece_quantity(order_qty, pieces_per_box)


def _purchase_qty(required_piece_qty: int, inventory_deducted_qty: int, cutting_mode: str | None) -> int:
    return purchase_sheet_quantity(
        required_piece_qty,
        inventory_deducted_qty,
        cutting_mode,
    )


class _PendingRequisitionReadContext:
    """Request-local facts for both ordinary and complex pending rows.

    A row can take the small direct calculation only after this context proves
    that none of the inventory/BOM facts exists for it.  Complex rows reuse the
    same request snapshot instead of issuing the established helpers once per
    line.  This is deliberately request-local rather than a cross-request cache.
    """

    def __init__(
        self,
        db: Session,
        rows: list[tuple],
        *,
        include_display_facts: bool = True,
    ) -> None:
        item_ids = [item.id for item, *_ in rows]
        self.material_by_id: dict[int, Material] = {}
        self._bom_snapshots_by_item_id: dict[
            int, list[SalesOrderItemBomComponent]
        ] = {}
        self._bom_effective_by_snapshot_id: dict[int, tuple[int, int]] = {}
        self._bom_reservations_by_snapshot_id: dict[
            int, list[tuple[InventoryReservation, OrderItemSemiRequirement | None]]
        ] = {}
        self._bom_active_requisition_components: dict[int, set[str]] = {}
        self._bom_parent_active_requisition_item_ids: set[int] = set()
        self._bom_batch_supported_item_ids: set[int] = set()
        self._semi_requirements_by_item_component: dict[
            tuple[int, str], OrderItemSemiRequirement
        ] = {}
        self._semi_reserved_by_item_component: dict[tuple[int, str], int] = {}
        self._active_semi_reservation_item_ids: set[int] = set()
        self._posted_completion_item_ids: set[int] = set()
        self._finished_lots_by_item_id: dict[int, list[InventoryLot]] = {}
        self._safe_semi_lots_by_item_component: dict[
            tuple[int, str], list[InventoryLot]
        ] = {}
        self._order_by_item_id = {item.id: order for item, order, *_ in rows}
        self._product_by_item_id = {
            item.id: product for item, _order, _customer, product in rows
        }
        self._ordinary_item_ids: set[int] = set()
        if not item_ids:
            return

        if include_display_facts:
            material_ids = {
                int(item.material_id) for item, *_ in rows if item.material_id
            }
            material_ids.update(
                int(product.material_id)
                for _item, _order, _customer, product in rows
                if product.material_id
            )
            if material_ids:
                self.material_by_id = {
                    material.id: material
                    for material in db.scalars(
                        select(Material).where(Material.id.in_(material_ids))
                    ).all()
                }

        bom_snapshots = db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))
            .order_by(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
        for snapshot in bom_snapshots:
            self._bom_snapshots_by_item_id.setdefault(
                int(snapshot.sales_order_item_id), []
            ).append(snapshot)
        complex_item_ids = set(self._bom_snapshots_by_item_id)
        # Telescoping lid boxes have two independent cover/base requirements.
        # Their aggregate values cannot use the ordinary whole-item formula.
        complex_item_ids.update(
            item.id
            for item, _order, _customer, product in rows
            if _is_telescoping_lid_box(product.box_style)
            and item.snapshot_base_report_length_mm
            and item.snapshot_base_report_width_mm
        )
        # A saved semi requirement or an active reservation may change both
        # the remaining pieces and the safe stock actions shown by the view.
        semi_requirements = db.scalars(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id.in_(item_ids)
            )
        ).all()
        requirement_by_id = {
            int(requirement.id): requirement for requirement in semi_requirements
        }
        snapshot_ids = [int(snapshot.id) for snapshot in bom_snapshots]
        requirement_ids = list(requirement_by_id)
        for requirement in semi_requirements:
            if requirement.sales_order_item_bom_component_id is None:
                self._semi_requirements_by_item_component[
                    (int(requirement.order_item_id), requirement.component_type)
                ] = requirement
        complex_item_ids.update(
            int(requirement.order_item_id) for requirement in semi_requirements
        )

        reservation_scope = [InventoryReservation.order_item_id.in_(item_ids)]
        if snapshot_ids:
            reservation_scope.append(
                InventoryReservation.sales_order_item_bom_component_id.in_(
                    snapshot_ids
                )
            )
        if requirement_ids:
            reservation_scope.append(
                InventoryReservation.semi_requirement_id.in_(requirement_ids)
            )
        reservations = db.scalars(
            select(InventoryReservation).where(
                or_(*reservation_scope),
                InventoryReservation.status != "cancelled",
            )
        ).all()
        for reservation in reservations:
            order_item_id = int(reservation.order_item_id or 0)
            if not order_item_id:
                continue
            if (
                int(reservation.reserved_stock_quantity or 0)
                > int(reservation.consumed_stock_quantity or 0)
                + int(reservation.released_stock_quantity or 0)
            ):
                complex_item_ids.add(order_item_id)
                if reservation.reservation_type == "semi_order":
                    self._active_semi_reservation_item_ids.add(order_item_id)
            requirement = requirement_by_id.get(
                int(reservation.semi_requirement_id or 0)
            )
            if (
                reservation.reservation_type == "semi_order"
                and requirement is not None
            ):
                key = (order_item_id, requirement.component_type)
                self._semi_reserved_by_item_component[key] = (
                    self._semi_reserved_by_item_component.get(key, 0)
                    + max(
                        int(reservation.credited_requirement_quantity or 0)
                        - int(reservation.released_requirement_quantity or 0),
                        0,
                    )
                )

        adjustment_totals: dict[int, tuple[int, int]] = {}
        if snapshot_ids:
            adjustment_totals = {
                int(snapshot_id): (int(delta_sets or 0), int(delta_pieces or 0))
                for snapshot_id, delta_sets, delta_pieces in db.execute(
                    select(
                        SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id,
                        func.coalesce(
                            func.sum(
                                SalesOrderItemBomDemandAdjustment.delta_order_set_quantity
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
                            ),
                            0,
                        ),
                    )
                    .where(
                        SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.in_(
                            snapshot_ids
                        )
                    )
                    .group_by(
                        SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
                    )
                ).all()
            }
            supported_items = set(self._bom_snapshots_by_item_id)
            for snapshot in bom_snapshots:
                delta_sets, delta_pieces = adjustment_totals.get(
                    int(snapshot.id), (0, 0)
                )
                try:
                    require_positive_integer(
                        snapshot.quantity_per_set,
                        label="组件每套用量",
                    )
                    effective_sets = int(snapshot.order_set_quantity) + delta_sets
                    required_pieces = (
                        require_positive_integer(
                            snapshot.required_piece_quantity,
                            label="组件需求件数",
                        )
                        + delta_pieces
                    )
                    if (
                        effective_sets < 0
                        or required_pieces <= 0
                    ):
                        raise ValueError
                except (CompositeBOMExecutionError, TypeError, ValueError):
                    supported_items.discard(int(snapshot.sales_order_item_id))
                    continue
                self._bom_effective_by_snapshot_id[int(snapshot.id)] = (
                    effective_sets,
                    required_pieces,
                )
            self._bom_batch_supported_item_ids = supported_items

            snapshot_by_id = {
                int(snapshot.id): snapshot for snapshot in bom_snapshots
            }
            for reservation in reservations:
                requirement = requirement_by_id.get(
                    int(reservation.semi_requirement_id or 0)
                )
                snapshot_id = int(
                    reservation.sales_order_item_bom_component_id
                    or (
                        requirement.sales_order_item_bom_component_id
                        if requirement is not None
                        else 0
                    )
                    or 0
                )
                if snapshot_id in snapshot_by_id:
                    self._bom_reservations_by_snapshot_id.setdefault(
                        snapshot_id, []
                    ).append((reservation, requirement))

            for snapshot_id, component_type in db.execute(
                select(
                    RequisitionItemBomSource.sales_order_item_bom_component_id,
                    RequisitionItemBomSource.component_type,
                )
                .join(
                    RequisitionItem,
                    RequisitionItem.id
                    == RequisitionItemBomSource.requisition_item_id,
                )
                .where(
                    RequisitionItemBomSource.sales_order_item_bom_component_id.in_(
                        snapshot_ids
                    ),
                    func.lower(RequisitionItem.status).notin_(
                        INACTIVE_REQUISITION_ITEM_STATUSES
                    ),
                )
            ).all():
                self._bom_active_requisition_components.setdefault(
                    int(snapshot_id), set()
                ).add((component_type or "whole").strip().lower())

            linked_source = (
                select(RequisitionItemBomSource.id)
                .where(
                    RequisitionItemBomSource.requisition_item_id
                    == RequisitionItem.id
                )
                .exists()
            )
            self._bom_parent_active_requisition_item_ids = set(
                int(order_item_id)
                for order_item_id in db.scalars(
                    select(RequisitionItem.order_item_id).where(
                        RequisitionItem.order_item_id.in_(item_ids),
                        func.lower(RequisitionItem.status).notin_(
                            INACTIVE_REQUISITION_ITEM_STATUSES
                        ),
                        ~linked_source,
                    )
                ).all()
                if order_item_id is not None
            )

        if include_display_facts:
            self._posted_completion_item_ids = set(
                int(item_id)
                for item_id in db.scalars(
                    select(ProductionCompletion.order_item_id).where(
                        ProductionCompletion.order_item_id.in_(item_ids),
                        ProductionCompletion.status == "posted",
                    )
                ).all()
            )

            customer_ids = {
                int(order.customer_id) for _item, order, *_ in rows
            }
            product_ids = {
                int(product.id) for _item, _order, _customer, product in rows
            }
            finished_lots = db.scalars(
                select(InventoryLot)
                .options(
                    selectinload(InventoryLot.finished_detail),
                    selectinload(InventoryLot.location),
                )
                .join(
                    FinishedGoodsInventoryDetail,
                    FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
                )
                .where(
                    InventoryLot.inventory_type == "finished",
                    InventoryLot.status == "active",
                    InventoryLot.quantity_available > 0,
                    FinishedGoodsInventoryDetail.product_id.in_(product_ids),
                    FinishedGoodsInventoryDetail.owner_customer_id.in_(customer_ids),
                    FinishedGoodsInventoryDetail.is_general.is_(False),
                )
            ).all()
            finished_by_key: dict[tuple[int, int, str], list[InventoryLot]] = {}
            for lot in sorted(finished_lots, key=inventory_fifo_sort_key):
                detail = lot.finished_detail
                if detail is None or detail.owner_customer_id is None:
                    continue
                key = (
                    int(detail.owner_customer_id),
                    int(detail.product_id),
                    (detail.inventory_code_snapshot or "").strip(),
                )
                finished_by_key.setdefault(key, []).append(lot)

            semi_lots = db.scalars(
                select(InventoryLot)
                .options(
                    selectinload(InventoryLot.semi_finished_detail),
                    selectinload(InventoryLot.location),
                    selectinload(InventoryLot.allowed_products),
                )
                .join(
                    SemiFinishedInventoryDetail,
                    SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
                )
                .where(
                    InventoryLot.inventory_type == "semi_finished",
                    InventoryLot.status == "active",
                    InventoryLot.quantity_available > 0,
                    SemiFinishedInventoryDetail.owner_customer_id.in_(customer_ids),
                )
            ).all()
            semi_by_customer_component: dict[
                tuple[int, str], list[InventoryLot]
            ] = {}
            for lot in sorted(semi_lots, key=inventory_fifo_sort_key):
                detail = lot.semi_finished_detail
                if detail is None or detail.owner_customer_id is None:
                    continue
                semi_by_customer_component.setdefault(
                    (int(detail.owner_customer_id), detail.component_type), []
                ).append(lot)

            for item, order, _customer, product in rows:
                expected_code = (
                    item.snapshot_product_code or product.product_code or ""
                ).strip()
                finished_candidates = finished_by_key.get(
                    (int(order.customer_id), int(product.id), expected_code), []
                )
                if (
                    item.requisition_status == "未报料"
                    and item.id not in self._posted_completion_item_ids
                    and finished_candidates
                ):
                    self._finished_lots_by_item_id[item.id] = finished_candidates
                    complex_item_ids.add(item.id)

                for spec in _semi_component_specs_for_requisition(item, product):
                    component = str(spec["component_type"])
                    safe_lots = self._safe_semi_lots(
                        item=item,
                        order=order,
                        product=product,
                        component=component,
                        spec=spec,
                        candidate_lots=semi_by_customer_component.get(
                            (int(order.customer_id), component), []
                        ),
                    )
                    if safe_lots:
                        self._safe_semi_lots_by_item_component[
                            (item.id, component)
                        ] = safe_lots
                        complex_item_ids.add(item.id)

        self._ordinary_item_ids = set(item_ids) - {
            int(item_id) for item_id in complex_item_ids if item_id is not None
        }

    def is_ordinary(self, item: OrderItem) -> bool:
        return item.id in self._ordinary_item_ids

    def material_for(self, item: OrderItem) -> Material | None:
        return self.material_by_id.get(int(item.material_id)) if item.material_id else None

    def semi_reserved_piece_qty(self, item_id: int, component: str) -> int:
        return int(
            self._semi_reserved_by_item_component.get(
                (int(item_id), (component or "whole").strip().lower()),
                0,
            )
        )

    def bom_snapshots_for(
        self, item: OrderItem
    ) -> list[SalesOrderItemBomComponent]:
        return list(self._bom_snapshots_by_item_id.get(item.id, []))

    def _bom_inventory_coverage(
        self,
        snapshot: SalesOrderItemBomComponent,
        component: str,
    ) -> dict[str, int]:
        finished = 0
        semi = 0
        for reservation, requirement in self._bom_reservations_by_snapshot_id.get(
            int(snapshot.id), []
        ):
            credited = max(
                int(reservation.credited_requirement_quantity or 0)
                - int(reservation.released_requirement_quantity or 0),
                0,
            )
            if reservation.reservation_type == "finished_order":
                physical_pieces = (
                    _bom_snapshot_physical_pieces_per_component(
                        snapshot, component
                    )
                    if component == "whole"
                    else 1
                )
                finished += credited * physical_pieces
                continue
            if reservation.reservation_type != "semi_order":
                continue
            requirement_component = (
                requirement.component_type if requirement is not None else None
            )
            if component == "whole":
                if requirement_component not in {None, "whole"}:
                    continue
            elif requirement_component not in {None, "whole", component}:
                continue
            semi += credited
        return {
            "finished_piece_quantity": finished,
            "semi_piece_quantity": semi,
            "total_piece_quantity": finished + semi,
        }

    def _bom_has_active_requisition(
        self,
        snapshot: SalesOrderItemBomComponent,
        component: str,
    ) -> bool:
        accepted = (
            {component, "whole"}
            if component in {"cover", "base"}
            else {"whole"}
        )
        return bool(
            self._bom_active_requisition_components.get(
                int(snapshot.id), set()
            )
            & accepted
        )

    def bom_pending_component_requirements(
        self,
        db: Session,
        item: OrderItem,
        *,
        snapshots: list[SalesOrderItemBomComponent],
    ) -> list[dict]:
        if item.id not in self._bom_batch_supported_item_ids:
            return _bom_pending_component_requirements(
                db, item, snapshots=snapshots
            )
        rows: list[dict] = []
        for snapshot in snapshots:
            effective_sets, required_pieces = self._bom_effective_by_snapshot_id[
                int(snapshot.id)
            ]
            for component in _bom_snapshot_component_types(snapshot):
                requirement = _bom_snapshot_requirements(
                    db,
                    snapshot,
                    component_type=component,
                    effective_sets_override=effective_sets,
                    required_piece_quantity_override=required_pieces,
                    inventory_coverage_override=self._bom_inventory_coverage(
                        snapshot, component
                    ),
                )
                requirement["source_kind"] = "component"
                requirement["source_key"] = (
                    f"component:{snapshot.id}:{component}"
                )
                requirement["parent_order_item_id"] = item.id
                requirement["already_requisitioned"] = (
                    self._bom_has_active_requisition(snapshot, component)
                )
                requirement["can_requisition"] = (
                    int(requirement["remaining_required_piece_qty"]) > 0
                    and not requirement["already_requisitioned"]
                )
                rows.append(requirement)
        return rows

    def bom_pending_parent_requirement(
        self,
        item: OrderItem,
        *,
        finished_reserved_qty: int,
    ) -> dict:
        requirements = _current_requisition_requirements(
            None,
            item,
            cutting_mode=item.special_process,
            pieces_per_box=1,
            finished_reserved_qty=finished_reserved_qty,
            semi_reserved_piece_qty=self._semi_reserved_by_item_component.get(
                (item.id, "whole"), 0
            ),
        )
        already_requisitioned = (
            item.id in self._bom_parent_active_requisition_item_ids
        )
        return {
            "source_kind": "parent",
            "source_key": f"parent:{item.id}",
            "snapshot_id": None,
            "parent_order_item_id": item.id,
            "product_code": item.snapshot_product_code,
            "product_name": item.snapshot_product_name,
            "specification": item.snapshot_spec,
            "material": item.snapshot_material,
            "supplier_name": item.snapshot_supplier_name,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "report_length_mm": item.snapshot_report_length_mm,
            "report_width_mm": item.snapshot_report_width_mm,
            "required_piece_quantity": int(requirements["required_piece_qty"]),
            "remaining_required_piece_qty": int(
                requirements["remaining_required_piece_qty"]
            ),
            "semi_finished_reserved_piece_qty": int(
                requirements["semi_finished_reserved_piece_qty"]
            ),
            "requisition_qty": int(requirements["requisition_qty"]),
            "cutting_mode": str(requirements["cutting_mode"]),
            "cutting_factor": int(requirements["cutting_factor"]),
            "yield_per_sheet": int(requirements["cutting_factor"]),
            "already_requisitioned": already_requisitioned,
            "can_requisition": (
                int(requirements["remaining_required_piece_qty"]) > 0
                and not already_requisitioned
            ),
        }

    def _regular_semi_requirement(
        self, item: OrderItem, component: str
    ) -> OrderItemSemiRequirement | None:
        return self._semi_requirements_by_item_component.get(
            (item.id, component)
        )

    def current_requisition_summary(
        self,
        item: OrderItem,
        *,
        product: Product,
        finished_reserved_qty: int,
        cutting_mode: str | None = None,
    ) -> dict:
        return _current_requisition_summary(
            None,
            item,
            product=product,
            cutting_mode=cutting_mode,
            finished_reserved_qty=finished_reserved_qty,
            semi_reserved_by_component={
                component: self._semi_reserved_by_item_component.get(
                    (item.id, component), 0
                )
                for component in ("whole", "cover", "base")
            },
        )

    def late_finished_inventory_preview(
        self,
        *,
        item: OrderItem,
        requirements: dict,
    ) -> dict:
        blocked_reason = (
            "订单已有半成品或客户专用纸板备料预占"
            if item.id in self._active_semi_reservation_item_ids
            else None
        )
        candidates = (
            []
            if blocked_reason is not None
            else self._finished_lots_by_item_id.get(item.id, [])
        )
        return _late_finished_inventory_preview_from_facts(
            requirements=requirements,
            candidates=candidates,
            blocked_reason=blocked_reason,
        )

    def customer_board_preparation_summary(
        self,
        *,
        item: OrderItem,
        product: Product,
    ) -> dict[str, int | bool]:
        available_piece_quantity = 0
        available_sheet_quantity = 0
        has_option = False
        for spec in _semi_component_specs_for_requisition(item, product):
            component = str(spec["component_type"])
            length = spec.get("board_length_mm")
            width = spec.get("board_width_mm")
            material_code = (item.snapshot_material or "").strip()
            flute_type = (item.flute_type or "").strip().upper()
            if not (length and width and material_code and flute_type):
                continue
            requirement = self._regular_semi_requirement(item, component)
            stock_yield = (
                int(requirement.stock_yield_per_sheet or 1)
                if requirement is not None
                else _cutting_factor(item.special_process)
            )
            current = _current_requisition_requirements(
                None,
                item,
                pieces_per_box=int(spec["pieces_per_box"]),
                component_type=component,
                finished_reserved_qty=0,
                semi_reserved_piece_qty=self._semi_reserved_by_item_component.get(
                    (item.id, component), 0
                ),
            )
            remaining = int(current["remaining_required_piece_qty"])
            if remaining <= 0:
                continue
            lots = self._safe_semi_lots_by_item_component.get(
                (item.id, component), []
            )
            available = min(
                remaining,
                sum(
                    int(lot.quantity_available or 0)
                    * int(lot.semi_finished_detail.stock_yield_per_sheet or 1)
                    for lot in lots
                    if lot.semi_finished_detail is not None
                ),
            )
            if available <= 0:
                continue
            has_option = True
            available_piece_quantity += available
            available_sheet_quantity += (
                available + stock_yield - 1
            ) // stock_yield
        return {
            "available_piece_quantity": available_piece_quantity,
            "available_sheet_quantity": available_sheet_quantity,
            "has_option": has_option,
        }

    def _safe_semi_lots(
        self,
        *,
        item: OrderItem,
        order: Order,
        product: Product,
        component: str,
        spec: dict,
        candidate_lots: list[InventoryLot],
    ) -> list[InventoryLot]:
        requirement = self._regular_semi_requirement(item, component)
        expected_length = int(
            requirement.board_length_mm
            if requirement is not None
            else spec.get("board_length_mm") or 0
        )
        expected_width = int(
            requirement.board_width_mm
            if requirement is not None
            else spec.get("board_width_mm") or 0
        )
        raw_material = (
            requirement.normalized_material_code
            if requirement is not None
            else (item.snapshot_material or "").strip()
        )
        if not expected_length or not expected_width or not raw_material:
            return []
        try:
            expected_material = normalize_material_code(raw_material)
        except WarehouseInventoryError:
            return []
        expected_flute = (
            requirement.flute_type
            if requirement is not None
            else (item.flute_type or "").strip().upper()
        )
        if not expected_flute:
            return []
        expected_pieces = int(
            requirement.pieces_per_box
            if requirement is not None
            else spec.get("pieces_per_box") or 1
        )
        expected_yield = int(
            requirement.stock_yield_per_sheet
            if requirement is not None
            else _cutting_factor(item.special_process)
        )
        material = (
            self.material_by_id.get(int(product.material_id))
            if product.material_id
            else None
        )
        expected_supplier = (
            item.snapshot_supplier_name
            or (material.supplier_name if material is not None else None)
            or ""
        ).strip()
        expected_layer_count = int(item.layer_count or product.layer_count or 0)
        crease_type, crease_left, crease_middle, crease_right = _component_crease(
            item, component
        )
        safe_lots: list[InventoryLot] = []
        for lot in candidate_lots:
            detail = lot.semi_finished_detail
            if detail is None:
                continue
            if int(product.id) not in {
                int(binding.product_id) for binding in lot.allowed_products
            }:
                continue
            if (
                int(detail.owner_customer_id or 0) != int(order.customer_id)
                or int(detail.board_length_mm or 0) != expected_length
                or int(detail.board_width_mm or 0) != expected_width
                or detail.normalized_material_code != expected_material
                or detail.flute_type != expected_flute
                or detail.component_type != component
                or int(detail.pieces_per_box or 0) != expected_pieces
                or int(detail.stock_yield_per_sheet or 0) != expected_yield
            ):
                continue
            if not safe_physical_board_facts_match(
                detail,
                supplier_name=expected_supplier,
                layer_count=expected_layer_count,
                crease_type=crease_type,
                crease_left_mm=crease_left,
                crease_middle_mm=crease_middle,
                crease_right_mm=crease_right,
            ):
                continue
            safe_lots.append(lot)
        return safe_lots


def _ordinary_requisition_requirements(
    item: OrderItem,
    *,
    cutting_mode: str | None = None,
) -> dict[str, int | str | bool]:
    """Exact direct formula after request-local negative-fact verification."""
    resolved_cutting_mode = normalize_cutting_mode(
        cutting_mode or item.special_process
    )
    pieces_per_box = _pieces_per_box(item)
    production_required_qty = max(int(item.quantity or 0), 0)
    required_piece_qty = _required_piece_qty(production_required_qty, pieces_per_box)
    requirement = {
        "cutting_mode": resolved_cutting_mode,
        "cutting_factor": _cutting_factor(resolved_cutting_mode),
        "pieces_per_box": pieces_per_box,
        "finished_inventory_reserved_qty": 0,
        "production_required_qty": production_required_qty,
        "fully_covered_by_finished_inventory": False,
        "required_piece_qty": required_piece_qty,
        "semi_finished_reserved_piece_qty": 0,
        "remaining_required_piece_qty": required_piece_qty,
        "requisition_qty": _purchase_qty(
            required_piece_qty,
            0,
            resolved_cutting_mode,
        ),
        "component_type": "whole",
    }
    summary = dict(requirement)
    summary["component_requirements"] = [dict(requirement)]
    return summary


def _bom_snapshots_for_order_item(
    db: Session,
    order_item_id: int,
) -> list[SalesOrderItemBomComponent]:
    return db.scalars(
        select(SalesOrderItemBomComponent)
        .where(SalesOrderItemBomComponent.sales_order_item_id == order_item_id)
        .order_by(
            SalesOrderItemBomComponent.display_order,
            SalesOrderItemBomComponent.id,
        )
    ).all()


def _bom_snapshot_requirements(
    db: Session,
    snapshot: SalesOrderItemBomComponent,
    *,
    component_type: str | None = None,
    cutting_mode: str | None = None,
    actual_yield_per_sheet: int | None = None,
    effective_sets_override: int | None = None,
    required_piece_quantity_override: int | None = None,
    inventory_coverage_override: dict[str, int] | None = None,
) -> dict:
    """Return one immutable BOM snapshot physical source requirement."""
    component = _bom_snapshot_component_type(snapshot, component_type)
    demand = None
    if (
        effective_sets_override is None
        or required_piece_quantity_override is None
    ):
        demand = next(
            (
                row
                for row in effective_component_demands(
                    db,
                    snapshot.sales_order_item_id,
                )
                if row.snapshot_id == snapshot.id
            ),
            None,
        )
        if demand is None:
            raise HTTPException(status_code=409, detail="组件需求快照不存在")
    effective_sets = int(
        effective_sets_override
        if effective_sets_override is not None
        else demand.effective_sets
    )
    try:
        if actual_yield_per_sheet is not None:
            actual_yield_per_sheet = require_positive_integer(
                actual_yield_per_sheet,
                label="实际模切出数",
            )
            if not snapshot.is_die_cut:
                raise CompositeBOMExecutionError("非模切组件不能填写实际模切出数")
            if snapshot.mold_max_yield_per_sheet is None:
                raise CompositeBOMExecutionError("模切组件缺少最大模切出数")
            if actual_yield_per_sheet > int(snapshot.mold_max_yield_per_sheet):
                raise CompositeBOMExecutionError("实际模切出数不能超过模具最大出数")
        quantity_per_set = require_positive_integer(
            snapshot.quantity_per_set,
            label="组件每套用量",
        )
    except CompositeBOMExecutionError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    allowed_cutting_mode = (
        (snapshot.snapshot_component_box_style or "").strip()
        in CUTTING_MODE_BOX_STYLES
    )
    frozen_cutting_mode = (
        snapshot.snapshot_component_default_cutting_mode or DEFAULT_CUTTING_MODE
    )
    resolved_cutting_mode = (
        (cutting_mode or frozen_cutting_mode)
        if allowed_cutting_mode
        else DEFAULT_CUTTING_MODE
    )
    resolved_cutting_mode = normalize_cutting_mode(resolved_cutting_mode)
    cutting_factor = _cutting_factor(resolved_cutting_mode)
    if actual_yield_per_sheet is not None:
        yield_per_sheet = actual_yield_per_sheet
    elif cutting_factor > 1:
        if (
            snapshot.is_die_cut
            and snapshot.mold_max_yield_per_sheet is not None
            and cutting_factor > int(snapshot.mold_max_yield_per_sheet)
        ):
            raise HTTPException(
                status_code=400,
                detail="默认开料每张产出不能超过模具最大出数",
            )
        yield_per_sheet = cutting_factor
    elif snapshot.is_die_cut and snapshot.mold_max_yield_per_sheet is not None:
        yield_per_sheet = int(snapshot.mold_max_yield_per_sheet)
    else:
        yield_per_sheet = 1
    coverage = (
        inventory_coverage_override
        if inventory_coverage_override is not None
        else component_inventory_coverage(
            db,
            snapshot.id,
            component_type=component,
        )
    )
    finished_reserved = coverage["finished_piece_quantity"]
    semi_reserved = coverage["semi_piece_quantity"]
    component_unit_quantity = int(
        required_piece_quantity_override
        if required_piece_quantity_override is not None
        else demand.required_piece_quantity
    )
    physical_pieces_per_component = (
        _bom_snapshot_physical_pieces_per_component(snapshot, component)
    )
    required_piece_quantity = (
        component_unit_quantity * physical_pieces_per_component
    )
    inventory_covered = min(
        coverage["total_piece_quantity"], required_piece_quantity
    )
    remaining = max(required_piece_quantity - inventory_covered, 0)
    net_sheets = (remaining + yield_per_sheet - 1) // yield_per_sheet
    requisition_qty = net_sheets + int(snapshot.spare_sheet_quantity or 0)
    is_base = component == "base"
    component_suffix = "底" if is_base else "盖" if component == "cover" else ""
    product_name = snapshot.snapshot_component_product_name
    if component_suffix:
        product_name = f"{product_name}-{component_suffix}"
    report_length_mm = (
        snapshot.snapshot_component_base_report_length_mm
        if is_base
        else snapshot.snapshot_component_report_length_mm
    )
    report_width_mm = (
        snapshot.snapshot_component_base_report_width_mm
        if is_base
        else snapshot.snapshot_component_report_width_mm
    )
    crease_type = (
        snapshot.snapshot_component_base_crease_type
        if is_base
        else snapshot.snapshot_component_crease_type
    )
    crease_left_mm = (
        snapshot.snapshot_component_base_crease_left_mm
        if is_base
        else snapshot.snapshot_component_crease_left_mm
    )
    crease_middle_mm = (
        snapshot.snapshot_component_base_crease_middle_mm
        if is_base
        else snapshot.snapshot_component_crease_middle_mm
    )
    crease_right_mm = (
        snapshot.snapshot_component_base_crease_right_mm
        if is_base
        else snapshot.snapshot_component_crease_right_mm
    )
    report_notes = (
        snapshot.snapshot_component_base_report_notes
        if is_base
        else snapshot.snapshot_component_report_notes
    )
    return {
        "snapshot_id": snapshot.id,
        "product_id": snapshot.component_product_id,
        "product_version": (
            component_product.version
            if (
                component_product := db.get(
                    Product,
                    snapshot.component_product_id,
                )
            )
            is not None
            else snapshot.component_product_version
        ),
        "material_id": snapshot.snapshot_component_material_id,
        "component_type": component,
        "effective_set_quantity": effective_sets,
        "quantity_per_set": quantity_per_set,
        "component_unit_quantity": component_unit_quantity,
        "physical_pieces_per_component": physical_pieces_per_component,
        "required_piece_quantity": required_piece_quantity,
        "finished_component_reserved_piece_qty": finished_reserved,
        "semi_finished_reserved_piece_qty": semi_reserved,
        "inventory_covered_piece_qty": inventory_covered,
        "remaining_required_piece_qty": remaining,
        "actual_yield_per_sheet": actual_yield_per_sheet,
        "yield_per_sheet": yield_per_sheet,
        "requisition_qty": requisition_qty,
        "spare_sheet_quantity": int(snapshot.spare_sheet_quantity or 0),
        "cutting_mode": resolved_cutting_mode,
        "cutting_factor": cutting_factor,
        "is_required": bool(snapshot.is_required),
        "display_order": snapshot.display_order,
        "product_code": snapshot.snapshot_component_product_code,
        "product_name": product_name,
        "specification": snapshot.snapshot_component_spec,
        "material": snapshot.snapshot_component_material,
        "layer_count": snapshot.snapshot_component_layer_count,
        "flute_type": snapshot.snapshot_component_flute_type,
        "supplier_name": snapshot.snapshot_component_supplier_name,
        "report_length_mm": report_length_mm,
        "report_width_mm": report_width_mm,
        "crease_type": crease_type,
        "crease_left_mm": crease_left_mm,
        "crease_middle_mm": crease_middle_mm,
        "crease_right_mm": crease_right_mm,
        "remark": snapshot.remark or report_notes,
    }


def _bom_snapshot_has_active_requisition(
    db: Session,
    snapshot_id: int,
    *,
    component_type: str = "whole",
) -> bool:
    component = (component_type or "").strip().lower()
    if component not in {"whole", "cover", "base"}:
        raise ValueError("invalid BOM requisition component type")
    accepted_types = (
        [component, "whole"] if component in {"cover", "base"} else ["whole"]
    )
    return (
        db.scalar(
            select(RequisitionItemBomSource.id)
            .join(
                RequisitionItem,
                RequisitionItem.id == RequisitionItemBomSource.requisition_item_id,
            )
            .where(
                RequisitionItemBomSource.sales_order_item_bom_component_id
                == snapshot_id,
                RequisitionItemBomSource.component_type.in_(accepted_types),
                func.lower(RequisitionItem.status).notin_(
                    INACTIVE_REQUISITION_ITEM_STATUSES
                ),
            )
            .limit(1)
        )
        is not None
    )


def _bom_snapshot_material_correction_blocker(
    db: Session,
    snapshot: SalesOrderItemBomComponent,
) -> str | None:
    if any(
        _bom_snapshot_has_active_requisition(
            db,
            snapshot.id,
            component_type=component_type,
        )
        for component_type in _bom_snapshot_component_types(snapshot)
    ):
        return "该组件已经正式报料，不能再更换供应商或材质"

    posted_receipt = db.scalar(
        select(IncomingReceiptItem.id)
        .join(
            RequisitionItemBomSource,
            RequisitionItemBomSource.requisition_item_id
            == IncomingReceiptItem.requisition_item_id,
        )
        .where(
            RequisitionItemBomSource.sales_order_item_bom_component_id
            == snapshot.id,
            IncomingReceiptItem.status == "posted",
        )
        .limit(1)
    )
    if posted_receipt is not None:
        return "该组件已经产生正式收料记录，不能再更换供应商或材质"

    has_component_reservation = db.scalar(
        select(InventoryReservation.id)
        .where(
            InventoryReservation.order_item_id == snapshot.sales_order_item_id,
            InventoryReservation.sales_order_item_bom_component_id
            == snapshot.id,
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .limit(1)
    )
    if has_component_reservation is not None:
        return "该组件已有未消耗库存预占，请先释放预占后再修改材质"

    completion = db.scalar(
        select(ProductionCompletion.id)
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .where(
            ProductionTask.sales_order_item_bom_component_id == snapshot.id,
        )
        .limit(1)
    )
    if completion is not None:
        return "该组件已经产生生产完工记录，不能再更换供应商或材质"
    return None


def _bom_snapshot_is_fully_requisitioned(
    db: Session,
    snapshot: SalesOrderItemBomComponent,
) -> bool:
    return all(
        _bom_snapshot_has_active_requisition(
            db,
            snapshot.id,
            component_type=component,
        )
        or int(
            _bom_snapshot_requirements(
                db,
                snapshot,
                component_type=component,
            )["remaining_required_piece_qty"]
        )
        == 0
        for component in _bom_snapshot_component_types(snapshot)
    )


def _bom_parent_has_active_requisition(
    db: Session,
    order_item_id: int,
) -> bool:
    linked_source = (
        select(RequisitionItemBomSource.id)
        .where(
            RequisitionItemBomSource.requisition_item_id == RequisitionItem.id
        )
        .exists()
    )
    return (
        db.scalar(
            select(RequisitionItem.id)
            .where(
                RequisitionItem.order_item_id == order_item_id,
                func.lower(RequisitionItem.status).notin_(
                    INACTIVE_REQUISITION_ITEM_STATUSES
                ),
                ~linked_source,
            )
            .limit(1)
        )
        is not None
    )


def _bom_order_item_is_fully_requisitioned(
    db: Session,
    item: OrderItem,
    snapshots: list[SalesOrderItemBomComponent],
) -> bool:
    parent_ready = (
        _composite_parent_requisition_is_suppressed(item, snapshots)
        or _bom_parent_has_active_requisition(db, item.id)
        or int(
            _current_requisition_requirements(
                db,
                item,
            )["remaining_required_piece_qty"]
        )
        == 0
    )
    return parent_ready and all(
        _bom_snapshot_is_fully_requisitioned(db, snapshot)
        for snapshot in snapshots
    )


def _bom_pending_component_requirements(
    db: Session,
    item: OrderItem,
    *,
    snapshots: list[SalesOrderItemBomComponent] | None = None,
) -> list[dict]:
    rows: list[dict] = []
    resolved_snapshots = (
        snapshots
        if snapshots is not None
        else _bom_snapshots_for_order_item(db, item.id)
    )
    for snapshot in resolved_snapshots:
        for component_type in _bom_snapshot_component_types(snapshot):
            requirements = _bom_snapshot_requirements(
                db,
                snapshot,
                component_type=component_type,
            )
            requirements["source_kind"] = "component"
            requirements["source_key"] = (
                f"component:{snapshot.id}:{component_type}"
            )
            requirements["parent_order_item_id"] = item.id
            requirements["already_requisitioned"] = (
                _bom_snapshot_has_active_requisition(
                    db,
                    snapshot.id,
                    component_type=component_type,
                )
            )
            requirements["can_requisition"] = (
                requirements["remaining_required_piece_qty"] > 0
                and not requirements["already_requisitioned"]
            )
            rows.append(requirements)
    return rows


def _bom_pending_parent_requirement(
    db: Session,
    item: OrderItem,
) -> dict:
    requirements = _current_requisition_requirements(
        db,
        item,
        cutting_mode=item.special_process,
        pieces_per_box=1,
    )
    already_requisitioned = _bom_parent_has_active_requisition(db, item.id)
    return {
        "source_kind": "parent",
        "source_key": f"parent:{item.id}",
        "snapshot_id": None,
        "parent_order_item_id": item.id,
        "product_code": item.snapshot_product_code,
        "product_name": item.snapshot_product_name,
        "specification": item.snapshot_spec,
        "material": item.snapshot_material,
        "supplier_name": item.snapshot_supplier_name,
        "layer_count": item.layer_count,
        "flute_type": item.flute_type,
        "report_length_mm": item.snapshot_report_length_mm,
        "report_width_mm": item.snapshot_report_width_mm,
        "required_piece_quantity": int(requirements["required_piece_qty"]),
        "remaining_required_piece_qty": int(
            requirements["remaining_required_piece_qty"]
        ),
        "semi_finished_reserved_piece_qty": int(
            requirements["semi_finished_reserved_piece_qty"]
        ),
        "requisition_qty": int(requirements["requisition_qty"]),
        "cutting_mode": str(requirements["cutting_mode"]),
        "cutting_factor": int(requirements["cutting_factor"]),
        "yield_per_sheet": int(requirements["cutting_factor"]),
        "already_requisitioned": already_requisitioned,
        "can_requisition": (
            int(requirements["remaining_required_piece_qty"]) > 0
            and not already_requisitioned
        ),
    }


def _current_requisition_requirements(
    db: Session | None,
    item: OrderItem,
    *,
    cutting_mode: str | None = None,
    pieces_per_box: int | None = None,
    finished_reserved_qty: int | None = None,
    component_type: str | None = None,
    semi_reserved_piece_qty: int | None = None,
) -> dict[str, int | str | bool]:
    """Derive current purchase demand from active finished-stock reservations."""
    resolved_cutting_mode = normalize_cutting_mode(
        cutting_mode or item.special_process
    )
    normalized_component = (component_type or "whole").strip().lower()
    resolved_pieces_per_box = max(
        int(
            1
            if normalized_component in {"cover", "base"}
            else (
                pieces_per_box
                if pieces_per_box is not None
                else _pieces_per_box(item)
            )
        ),
        1,
    )
    resolved_reserved_qty = max(
        int(
            finished_reserved_qty
            if finished_reserved_qty is not None
            else requisition_finished_inventory_coverage_qty(db, item.id)
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
    semi_finished_reserved_piece_qty = max(
        int(
            semi_reserved_piece_qty
            if semi_reserved_piece_qty is not None
            else active_semi_reserved_piece_qty(
                db,
                order_item_id=item.id,
                component_type=normalized_component,
            )
        ),
        0,
    )
    remaining_required_piece_qty = max(
        required_piece_qty - semi_finished_reserved_piece_qty, 0
    )
    return {
        "cutting_mode": resolved_cutting_mode,
        "cutting_factor": _cutting_factor(resolved_cutting_mode),
        "pieces_per_box": resolved_pieces_per_box,
        "finished_inventory_reserved_qty": resolved_reserved_qty,
        "production_required_qty": production_required_qty,
        "fully_covered_by_finished_inventory": production_required_qty == 0,
        "required_piece_qty": required_piece_qty,
        "semi_finished_reserved_piece_qty": semi_finished_reserved_piece_qty,
        "remaining_required_piece_qty": remaining_required_piece_qty,
        "requisition_qty": _purchase_qty(
            remaining_required_piece_qty,
            0,
            resolved_cutting_mode,
        ),
        "component_type": normalized_component,
    }


def _current_requisition_summary(
    db: Session | None,
    item: OrderItem,
    *,
    cutting_mode: str | None = None,
    finished_reserved_qty: int | None = None,
    product: Product | None = None,
    semi_reserved_by_component: dict[str, int] | None = None,
) -> dict:
    if product is None:
        if db is None:
            raise RuntimeError("缺少报料产品上下文")
        product = db.get(Product, item.product_id)
    component_types = (
        ["cover", "base"]
        if product is not None
        and _is_telescoping_lid_box(product.box_style)
        and item.snapshot_base_report_length_mm
        and item.snapshot_base_report_width_mm
        else ["whole"]
    )
    components = [
        _current_requisition_requirements(
            db,
            item,
            cutting_mode=cutting_mode,
            finished_reserved_qty=finished_reserved_qty,
            component_type=component_type,
            semi_reserved_piece_qty=(
                semi_reserved_by_component.get(component_type, 0)
                if semi_reserved_by_component is not None
                else None
            ),
        )
        for component_type in component_types
    ]
    first = dict(components[0])
    first.update(
        {
            "required_piece_qty": sum(
                int(row["required_piece_qty"]) for row in components
            ),
            "semi_finished_reserved_piece_qty": sum(
                int(row["semi_finished_reserved_piece_qty"])
                for row in components
            ),
            "remaining_required_piece_qty": sum(
                int(row["remaining_required_piece_qty"])
                for row in components
            ),
            "requisition_qty": sum(
                int(row["requisition_qty"]) for row in components
            ),
            "component_requirements": components,
        }
    )
    return first


def _requires_supplier_purchase(requirements: dict) -> bool:
    """Return whether a live requirement still needs a positive supplier purchase."""
    return (
        int(requirements.get("remaining_required_piece_qty") or 0) > 0
        and int(requirements.get("requisition_qty") or 0) > 0
    )


def _remaining_supplier_requisition_summary(
    db: Session,
    item: OrderItem,
    *,
    summary: dict | None = None,
) -> dict:
    """Subtract active formal reports without collapsing cover/base identity."""

    current = dict(summary or _current_requisition_summary(db, item))
    remaining_components: list[dict] = []
    theoretical_total = 0
    already_requisitioned_total = 0
    remaining_total = 0
    for requirement in current.get("component_requirements") or [current]:
        component = dict(requirement)
        component_type = str(
            component.get("component_type") or "whole"
        ).strip().lower()
        theoretical_qty = int(component.get("requisition_qty") or 0)
        active_facts = _active_supplier_requisition_facts(
            db,
            item=item,
            component_type=component_type,
        )
        already_requisitioned_qty = int(active_facts.get("quantity") or 0)
        remaining_qty = max(theoretical_qty - already_requisitioned_qty, 0)
        component.update(
            {
                "theoretical_requisition_qty": theoretical_qty,
                "already_requisitioned_qty": already_requisitioned_qty,
                "requisition_qty": remaining_qty,
            }
        )
        remaining_components.append(component)
        theoretical_total += theoretical_qty
        already_requisitioned_total += already_requisitioned_qty
        remaining_total += remaining_qty

    current.update(
        {
            "theoretical_requisition_qty": theoretical_total,
            "already_requisitioned_qty": already_requisitioned_total,
            "requisition_qty": remaining_total,
            "component_requirements": remaining_components,
        }
    )
    return current


_LATE_SEMI_REVIEWABLE_DIFFERENCES = {
    "pieces_per_box",
    "stock_yield_per_sheet",
}


def _semi_component_specs_for_requisition(
    item: OrderItem,
    product: Product,
    *,
    component_type: str | None = None,
) -> list[dict]:
    requested_component = (component_type or "").strip().lower()
    if requested_component:
        components = [requested_component]
    elif (
        _is_telescoping_lid_box(product.box_style)
        and item.snapshot_base_report_length_mm
        and item.snapshot_base_report_width_mm
    ):
        components = ["cover", "base"]
    else:
        components = ["whole"]

    specs: list[dict] = []
    for component in components:
        if component not in {"whole", "cover", "base"}:
            raise HTTPException(status_code=400, detail="半成品组件类型无效")
        is_base = component == "base"
        specs.append(
            {
                "component_type": component,
                "board_length_mm": (
                    item.snapshot_base_report_length_mm
                    if is_base
                    else item.snapshot_report_length_mm
                ),
                "board_width_mm": (
                    item.snapshot_base_report_width_mm
                    if is_base
                    else item.snapshot_report_width_mm
                ),
                "pieces_per_box": (
                    1 if component in {"cover", "base"} else _pieces_per_box(item)
                ),
            }
        )
    return specs


def _semi_candidate_dict_for_requisition(row: SemiFinishedCandidate) -> dict:
    lot = row.lot
    detail = lot.semi_finished_detail
    location = lot.location
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "version": lot.version,
        "source": row.source,
        "match_rule_id": row.match_rule_id,
        "available_stock_quantity": row.available_stock_quantity,
        "deductible_requirement_quantity": row.deductible_requirement_quantity,
        "warehouse_location": {
            "id": location.id,
            "location_code": location.location_code,
            "location_name": location.location_name,
        },
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "material_code": detail.material_code_snapshot,
        "flute_type": detail.flute_type,
        "component_type": detail.component_type,
        "pieces_per_box": detail.pieces_per_box,
        "stock_yield_per_sheet": detail.stock_yield_per_sheet,
        "signature_differences": list(row.signature_differences),
        "warning_codes": list(row.warning_codes),
        "warning_messages": list(row.warning_messages),
    }


def _late_semi_inventory_options(db: Session, entry: dict) -> list[dict]:
    item: OrderItem = entry["order_item"]
    product: Product = entry["product"]
    req_item: RequisitionItem | None = entry.get("req_item")
    requested_component = (
        _requisition_item_component(req_item) if req_item is not None else None
    )
    options: list[dict] = []
    for spec in _semi_component_specs_for_requisition(
        item,
        product,
        component_type=requested_component,
    ):
        component = str(spec["component_type"])
        length = spec.get("board_length_mm")
        width = spec.get("board_width_mm")
        material_code = (item.snapshot_material or "").strip()
        flute_type = (item.flute_type or "").strip().upper()
        if not (length and width and material_code and flute_type):
            continue
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item.id,
                OrderItemSemiRequirement.component_type == component,
            )
        )
        stock_yield = (
            int(requirement.stock_yield_per_sheet or 1)
            if requirement
            else _cutting_factor(entry.get("cutting_mode") or item.special_process)
        )
        requirements = _current_requisition_requirements(
            db,
            item,
            cutting_mode=entry.get("cutting_mode"),
            pieces_per_box=int(spec["pieces_per_box"]),
            component_type=component,
        )
        remaining = int(requirements["remaining_required_piece_qty"])
        if remaining <= 0:
            continue
        recommended = semi_finished_candidates_for_product(
            db,
            product_id=product.id,
            customer_id=entry["customer"].id,
            board_length_mm=int(length),
            board_width_mm=int(width),
            material_code=material_code,
            flute_type=flute_type,
            component_type=component,
            pieces_per_box=int(spec["pieces_per_box"]),
            stock_yield_per_sheet=stock_yield,
        )
        recommended_ids = {row.lot.id for row in recommended}
        review_candidates = [
            row
            for row in browse_semi_finished_inventory_for_product(
                db,
                product_id=product.id,
                customer_id=entry["customer"].id,
                board_length_mm=int(length),
                board_width_mm=int(width),
                material_code=material_code,
                flute_type=flute_type,
                component_type=component,
                pieces_per_box=int(spec["pieces_per_box"]),
                stock_yield_per_sheet=stock_yield,
            )
            if row.lot.id not in recommended_ids
            and set(row.signature_differences).issubset(
                _LATE_SEMI_REVIEWABLE_DIFFERENCES
            )
        ]
        if not recommended and not review_candidates:
            continue
        options.append(
            {
                "order_item_id": item.id,
                "order_number": display_order_number(
                    entry["order"], entry.get("display_registry") or {}
                ),
                "product_code": (
                    req_item.product_code_snapshot
                    if req_item is not None
                    else item.snapshot_product_code or product.product_code
                ),
                "product_name": (
                    req_item.product_name_snapshot
                    if req_item is not None
                    else item.snapshot_product_name
                ),
                "requirement_id": requirement.id if requirement else None,
                "component_type": component,
                "required_piece_quantity": int(requirements["required_piece_qty"]),
                "reserved_piece_quantity": int(
                    requirements["semi_finished_reserved_piece_qty"]
                ),
                "remaining_requirement_quantity": remaining,
                "recommended_candidates": [
                    _semi_candidate_dict_for_requisition(row) for row in recommended
                ],
                "review_candidates": [
                    _semi_candidate_dict_for_requisition(row)
                    for row in review_candidates
                ],
            }
        )
    return options


def _safe_late_finished_inventory_candidates(
    db: Session,
    *,
    item: OrderItem,
    order: Order,
    product: Product,
) -> list[InventoryLot]:
    """Return only exact customer-owned finished lots safe for one-click use."""

    expected_product_code = (item.snapshot_product_code or "").strip()
    if (
        not expected_product_code
        or item.product_id != product.id
        or product.customer_id != order.customer_id
    ):
        return []
    rows: list[InventoryLot] = []
    try:
        candidate_lots = finished_inventory_candidates(db, item.id)
    except WarehouseInventoryError as error:
        # A reported order is intentionally blocked from a new inventory
        # deduction.  This read-only preview must not turn that business gate
        # into a 500 for the pending-requisition list or dashboard.
        if error.status_code == 409:
            return []
        raise
    for lot in candidate_lots:
        detail = lot.finished_detail
        if (
            detail is None
            or detail.is_general
            or detail.owner_customer_id != order.customer_id
            or detail.product_id != item.product_id
            or (detail.inventory_code_snapshot or "").strip()
            != expected_product_code
        ):
            continue
        rows.append(lot)
    rows.sort(key=inventory_fifo_sort_key)
    return rows


def _late_finished_inventory_preview_from_facts(
    *,
    requirements: dict,
    candidates: list[InventoryLot],
    blocked_reason: str | None,
) -> dict:
    remaining_order_quantity = int(requirements["production_required_qty"])
    available_quantity = sum(
        max(int(lot.quantity_available or 0), 0) for lot in candidates
    )
    reservable_quantity = min(available_quantity, remaining_order_quantity)

    location_map: dict[int | None, dict] = {}
    lots: list[dict] = []
    for lot in candidates:
        location = lot.location
        location_id = location.id if location is not None else None
        quantity = max(int(lot.quantity_available or 0), 0)
        location_row = location_map.setdefault(
            location_id,
            {
                "location_id": location_id,
                "location_code": (
                    location.location_code if location is not None else "未设置"
                ),
                "location_name": (
                    location.location_name if location is not None else "未设置库位"
                ),
                "available_quantity": 0,
            },
        )
        location_row["available_quantity"] += quantity
        lots.append(
            {
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "version": lot.version,
                "available_quantity": quantity,
                "stock_date": lot.stock_date.isoformat() if lot.stock_date else None,
                "location_id": location_id,
                "location_code": location_row["location_code"],
                "location_name": location_row["location_name"],
            }
        )

    return {
        "available_quantity": available_quantity,
        "reservable_quantity": reservable_quantity,
        "remaining_order_quantity": remaining_order_quantity,
        "can_auto_reserve": (
            blocked_reason is None and reservable_quantity > 0
        ),
        "blocked_reason": blocked_reason,
        "locations": list(location_map.values()),
        "lots": lots,
    }


def _late_finished_inventory_preview(
    db: Session,
    *,
    item: OrderItem,
    order: Order,
    product: Product,
) -> dict:
    """Describe exact late finished stock without mutating inventory."""

    active_semi_reservation = db.scalar(
        select(InventoryReservation.id)
        .where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .limit(1)
    )
    blocked_reason: str | None = None
    if _bom_pending_component_requirements(db, item):
        blocked_reason = "组合产品须按父件和组件分别处理库存"
    elif active_semi_reservation is not None:
        blocked_reason = "订单已有半成品或客户专用纸板备料预占"

    candidates = (
        []
        if blocked_reason is not None
        else _safe_late_finished_inventory_candidates(
            db,
            item=item,
            order=order,
            product=product,
        )
    )
    return _late_finished_inventory_preview_from_facts(
        requirements=_current_requisition_summary(db, item),
        candidates=candidates,
        blocked_reason=blocked_reason,
    )


def _require_late_finished_inventory_resolved(
    db: Session,
    *,
    item: OrderItem,
    order: Order,
    product: Product,
    message_prefix: str = "发现订单保存后新增的同客户同存货编码成品库存",
) -> dict:
    preview = _late_finished_inventory_preview(
        db,
        item=item,
        order=order,
        product=product,
    )
    if preview["can_auto_reserve"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{message_prefix}，当前可抵扣 "
                f"{preview['reservable_quantity']} 个；"
                "请返回待报料列表先点击“使用成品，剩余再报”后重试。"
            ),
        )
    return preview


def _safe_customer_board_preparation_options(
    db: Session,
    *,
    item: OrderItem,
    order: Order,
    product: Product,
) -> list[dict]:
    """Find only exact customer-owned board-preparation lots safe for one-click use."""

    expected_supplier = (
        item.snapshot_supplier_name
        or (product.material.supplier_name if product.material is not None else None)
        or ""
    ).strip()
    expected_layer_count = int(item.layer_count or product.layer_count or 0)

    def is_exact_physical_match(
        candidate: SemiFinishedCandidate,
        *,
        component: str,
    ) -> bool:
        detail = candidate.lot.semi_finished_detail
        if detail is None:
            return False
        crease_type, crease_left, crease_middle, crease_right = _component_crease(
            item, component
        )
        return safe_physical_board_facts_match(
            detail,
            supplier_name=expected_supplier,
            layer_count=expected_layer_count,
            crease_type=crease_type,
            crease_left_mm=crease_left,
            crease_middle_mm=crease_middle,
            crease_right_mm=crease_right,
        )

    options: list[dict] = []
    for spec in _semi_component_specs_for_requisition(item, product):
        component = str(spec["component_type"])
        length = spec.get("board_length_mm")
        width = spec.get("board_width_mm")
        material_code = (item.snapshot_material or "").strip()
        flute_type = (item.flute_type or "").strip().upper()
        if not (length and width and material_code and flute_type):
            continue
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item.id,
                OrderItemSemiRequirement.sales_order_item_bom_component_id.is_(None),
                OrderItemSemiRequirement.component_type == component,
            )
        )
        stock_yield = (
            int(requirement.stock_yield_per_sheet or 1)
            if requirement is not None
            else _cutting_factor(item.special_process)
        )
        current = _current_requisition_requirements(
            db,
            item,
            pieces_per_box=int(spec["pieces_per_box"]),
            component_type=component,
        )
        remaining = int(current["remaining_required_piece_qty"])
        if remaining <= 0:
            continue
        candidates = (
            semi_finished_inventory_candidates(db, requirement.id)
            if requirement is not None
            else semi_finished_candidates_for_product(
                db,
                product_id=product.id,
                customer_id=order.customer_id,
                board_length_mm=int(length),
                board_width_mm=int(width),
                material_code=material_code,
                flute_type=flute_type,
                component_type=component,
                pieces_per_box=int(spec["pieces_per_box"]),
                stock_yield_per_sheet=stock_yield,
            )
        )
        safe_candidates = [
            row
            for row in candidates
            if (
                row.lot.semi_finished_detail is not None
                and row.lot.semi_finished_detail.owner_customer_id
                == order.customer_id
                and not row.signature_differences
                and row.source in {"signature", "learned"}
                and is_exact_physical_match(row, component=component)
            )
        ]
        available_piece_quantity = min(
            remaining,
            sum(
                int(row.deductible_requirement_quantity or 0)
                for row in safe_candidates
            ),
        )
        if available_piece_quantity <= 0:
            continue
        options.append(
            {
                "component_type": component,
                "board_length_mm": int(length),
                "board_width_mm": int(width),
                "material_code": material_code,
                "flute_type": flute_type,
                "pieces_per_box": int(spec["pieces_per_box"]),
                "stock_yield_per_sheet": stock_yield,
                "required_piece_quantity": int(current["required_piece_qty"]),
                "remaining_piece_quantity": remaining,
                "available_piece_quantity": available_piece_quantity,
                "available_sheet_quantity": (
                    available_piece_quantity + stock_yield - 1
                )
                // stock_yield,
                "requirement": requirement,
                "candidates": safe_candidates,
            }
        )
    return options


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
    try:
        recommendation = recommend_box_type(
            box_style=product.box_style,
            length_mm=int(product.length_mm),
            width_mm=int(product.width_mm),
            height_mm=int(product.height_mm),
            splice_mode=product.splice_mode,
            flap_mm=product.flap_mm,
            crease_type=product.crease_type,
        )
    except BoxTypeRuleError:
        return None, None
    if not recommendation["auto_calculated"]:
        return None, None
    return (
        Decimal(int(recommendation["report_length_mm"])),
        Decimal(int(recommendation["report_width_mm"])),
    )


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


def _order_customer_for_item(
    db: Session, item: OrderItem, user: User
) -> tuple[Order, Customer]:
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, user, db)
    customer = db.get(Customer, order.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    return order, customer


def _candidate_or_404(
    db: Session, candidate_id: int
) -> CustomerMaterialCandidate:
    candidate = db.get(CustomerMaterialCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="客户材质候选不存在")
    return candidate


def _candidate_material_or_404(db: Session, material_id: int) -> Material:
    material = db.get(Material, material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=404, detail="候选材质不存在或已停用")
    return material


def _material_candidate_audit(
    db: Session,
    *,
    user: User,
    action: str,
    candidate_id: int,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource="CustomerMaterialCandidate",
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type="customer_material_candidate",
            entity_id=candidate_id,
            description=description,
        )
    )


def _allowed_customer_ids(user: User, db: Session) -> set[int] | None:
    """None denotes the existing admin/boss or all-customer access mode."""
    # A few legacy unit tests call endpoint functions directly with an opaque
    # dependency placeholder.  Real HTTP requests always receive a User from
    # PermissionChecker; preserve the old direct-call behavior for placeholders.
    if not isinstance(user, User):
        return None
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _require_order_item_customer_access(
    db: Session, item: OrderItem, user: User
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id).where(Order.id == item.order_id)
    )
    if customer_id is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(customer_id, user, db)


_REQUISITION_HOLD_ACTIVE = "active"
_REQUISITION_HOLD_RELEASED = "released"
_REQUISITION_HOLD_INVALIDATED = "invalidated"
_REQUISITION_HOLD_ABNORMAL_ORDER_STATUSES = {
    "cancelled",
    "dead",
    "closed",
    "archived",
}


def _require_requisition_hold_customer_access(
    db: Session,
    *,
    hold: RequisitionHold,
    user: User,
) -> None:
    if hold.customer_id_snapshot is None:
        raise HTTPException(status_code=404, detail="等候报料记录不存在")
    require_customer_access(int(hold.customer_id_snapshot), user, db)


def _requisition_hold_audit_state(hold: RequisitionHold) -> dict[str, object]:
    return {
        "status": hold.status,
        "release_mode": hold.release_mode,
        "previous_order_item_id": hold.previous_order_item_id_snapshot,
        "expected_requisition_date": hold.expected_requisition_date,
        "version": int(hold.version or 0),
        "release_source": hold.release_source,
        "release_note": hold.release_note,
    }


def _append_requisition_hold_audit(
    db: Session,
    *,
    hold: RequisitionHold,
    user: User,
    action_code: str,
    legacy_action: str,
    result: str,
    source: str,
    description: str,
    transition_source: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    object_ref: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    details: dict[str, object] = {
        "hold_id": hold.id,
        "order_item_id": hold.order_item_id_snapshot,
        "result": result,
        "source": transition_source,
        "before": before,
        "after": after,
    }
    if extra:
        details.update(extra)
    append_audit_event(
        db,
        actor=user,
        event_category="business",
        result=result,
        source=source,
        module_code="requisition",
        action_code=action_code,
        legacy_action=legacy_action,
        resource="Requisition",
        entity_type="order_item",
        entity_id=hold.order_item_id_snapshot,
        object_ref=object_ref or f"requisition-hold:{hold.id}",
        customer_id=hold.customer_id_snapshot,
        customer_name=hold.customer_name_snapshot,
        description=description,
        details=details,
    )


def _append_requisition_hold_anomaly_once(
    db: Session,
    *,
    hold: RequisitionHold,
    user: User,
    warning: str,
) -> bool:
    object_ref = f"requisition-hold:{hold.id}:v{int(hold.version or 0)}:anomaly"
    existing_id = db.scalar(
        select(OperationLog.id)
        .where(
            OperationLog.action_code == "requisition.hold.anomaly",
            OperationLog.object_ref == object_ref,
        )
        .limit(1)
    )
    if existing_id is not None:
        return False
    current_state = _requisition_hold_audit_state(hold)
    _append_requisition_hold_audit(
        db,
        hold=hold,
        user=user,
        action_code="requisition.hold.anomaly",
        legacy_action="REQUISITION_HOLD_ANOMALY",
        result="failed",
        source="system",
        description=warning,
        transition_source="automatic_previous_batch_anomaly",
        before=current_state,
        after=current_state,
        object_ref=object_ref,
        extra={"warning": warning},
    )
    return True


def _active_requisition_hold(
    db: Session,
    order_item_id: int,
) -> RequisitionHold | None:
    return db.scalar(
        select(RequisitionHold)
        .where(
            RequisitionHold.order_item_id == order_item_id,
            RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
        )
        .limit(1)
    )


def _requisition_hold_live_row(
    db: Session,
    order_item_id: int,
    *,
    lock: bool = False,
) -> tuple[OrderItem, Order, Customer, Product]:
    query = (
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(OrderItem.id == order_item_id)
    )
    if lock:
        query = query.with_for_update()
    row = db.execute(query).first()
    if row is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    return row


def _order_item_still_requires_requisition(
    db: Session,
    item: OrderItem,
) -> bool:
    bom_components = _bom_pending_component_requirements(db, item)
    if bom_components:
        sources = [_bom_pending_parent_requirement(db, item), *bom_components]
        return any(bool(source.get("can_requisition")) for source in sources)
    return _requires_supplier_purchase(
        _remaining_supplier_requisition_summary(db, item)
    )


def _ensure_requisition_hold_eligible(
    db: Session,
    order_item_id: int,
    *,
    user: User,
    allow_existing_hold: bool = False,
    lock: bool = False,
) -> tuple[OrderItem, Order, Customer, Product]:
    item, order, customer, product = _requisition_hold_live_row(
        db, order_item_id, lock=lock
    )
    require_customer_access(order.customer_id, user, db)
    if is_history_order_number(order.order_number):
        raise HTTPException(status_code=409, detail="历史订单不能设置等候报料")
    if order.status in _REQUISITION_HOLD_ABNORMAL_ORDER_STATUSES:
        raise HTTPException(status_code=409, detail="已取消、死单、结单或归档订单不能设置等候报料")
    if item.is_force_closed:
        raise HTTPException(status_code=409, detail="强制结档明细不能设置等候报料")
    if item.material_status != "pending":
        raise HTTPException(status_code=409, detail="已有来料事实，不能设置等候报料")
    if item.requisition_status not in {"未报料", "已报料"}:
        raise HTTPException(
            status_code=409,
            detail="订单明细当前不在待报料范围，不能设置等候报料",
        )
    if _order_item_in_merged_pending_group(db, item.id):
        raise HTTPException(status_code=409, detail="订单明细已在合并报料草稿中，请先退出合并组")
    if not allow_existing_hold and _active_requisition_hold(db, item.id) is not None:
        raise HTTPException(status_code=409, detail="订单明细已经在等候报料中")
    if not _order_item_still_requires_requisition(db, item):
        active_facts = _active_requisition_facts_by_item_ids(db, [item.id]).get(
            item.id, {}
        )
        if int(active_facts.get("quantity") or 0) > 0:
            raise HTTPException(
                status_code=409,
                detail="当前采购需求已经全部正式报料，没有剩余数量可转入等候报料",
            )
        raise HTTPException(status_code=409, detail="该明细已由库存覆盖，无需再报料")
    _ensure_order_item_crease_width(item)
    return item, order, customer, product


def _previous_batch_state(
    db: Session,
    hold: RequisitionHold,
) -> dict:
    previous_item = (
        db.get(OrderItem, hold.previous_order_item_id)
        if hold.previous_order_item_id is not None
        else None
    )
    if previous_item is None:
        return {
            "state": "abnormal",
            "warning": "关联的上一批已不存在，请修改等候条件或手动恢复待报料",
            "item_id": hold.previous_order_item_id_snapshot,
            "ordered_quantity": None,
            "delivered_quantity": None,
            "remaining_quantity": None,
        }
    previous_order = db.get(Order, previous_item.order_id)
    if previous_order is None:
        return {
            "state": "abnormal",
            "warning": "关联的上一批订单已不存在，请修改等候条件或手动恢复待报料",
            "item_id": hold.previous_order_item_id_snapshot,
            "ordered_quantity": int(previous_item.quantity or 0),
            "delivered_quantity": int(previous_item.delivered_quantity or 0),
            "remaining_quantity": max(
                int(previous_item.quantity or 0)
                - int(previous_item.delivered_quantity or 0),
                0,
            ),
        }
    ordered_quantity = int(previous_item.quantity or 0)
    delivered_quantity = int(previous_item.delivered_quantity or 0)
    remaining_quantity = max(ordered_quantity - delivered_quantity, 0)
    abnormal = (
        previous_item.is_force_closed
        or previous_order.status in _REQUISITION_HOLD_ABNORMAL_ORDER_STATUSES
    )
    if abnormal:
        warning = (
            "上一批已短送结档，请修改等候条件或手动恢复待报料"
            if previous_item.is_force_closed and remaining_quantity > 0
            else "上一批已取消或结档，请修改等候条件或手动恢复待报料"
        )
        state = "abnormal"
    elif remaining_quantity == 0:
        warning = None
        state = "completed"
    else:
        warning = None
        state = "waiting"
    return {
        "state": state,
        "warning": warning,
        "item_id": previous_item.id,
        "item_sequence": previous_item.item_sequence,
        "item_order_number": previous_item.item_order_number,
        "order_number": previous_order.order_number,
        "order_date": previous_order.order_date,
        "delivery_date": previous_order.delivery_date,
        "product_code": previous_item.snapshot_product_code,
        "product_name": previous_item.snapshot_product_name,
        "ordered_quantity": ordered_quantity,
        "delivered_quantity": delivered_quantity,
        "remaining_quantity": remaining_quantity,
    }


def _previous_batch_candidates(
    db: Session,
    *,
    item: OrderItem,
    order: Order,
) -> list[dict]:
    product_code = (item.snapshot_product_code or "").strip()
    if not product_code:
        return []
    rows = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.customer_id == order.customer_id,
            OrderItem.id != item.id,
            func.trim(OrderItem.snapshot_product_code) == product_code,
            or_(
                OrderItem.created_at < item.created_at,
                and_(
                    OrderItem.created_at == item.created_at,
                    OrderItem.id < item.id,
                ),
            ),
            OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.is_force_closed.is_(False),
            Order.status.notin_(_REQUISITION_HOLD_ABNORMAL_ORDER_STATUSES),
        )
        .order_by(OrderItem.created_at.desc(), OrderItem.id.desc())
        .limit(20)
    ).all()
    candidates = []
    for previous_item, previous_order in rows:
        candidates.append(
            {
                "order_item_id": previous_item.id,
                "item_sequence": previous_item.item_sequence,
                "item_order_number": previous_item.item_order_number,
                "order_date": previous_order.order_date,
                "delivery_date": previous_order.delivery_date,
                "product_code": previous_item.snapshot_product_code,
                "product_name": previous_item.snapshot_product_name,
                "specification": previous_item.snapshot_spec,
                "material": previous_item.snapshot_material,
                "flute_type": previous_item.flute_type,
                "current_product_name": item.snapshot_product_name,
                "current_specification": item.snapshot_spec,
                "current_material": item.snapshot_material,
                "current_flute_type": item.flute_type,
                "ordered_quantity": int(previous_item.quantity or 0),
                "delivered_quantity": int(previous_item.delivered_quantity or 0),
                "remaining_quantity": max(
                    int(previous_item.quantity or 0)
                    - int(previous_item.delivered_quantity or 0),
                    0,
                ),
                "specification_changed": (
                    (previous_item.snapshot_spec or "").strip()
                    != (item.snapshot_spec or "").strip()
                ),
                "product_name_changed": (
                    (previous_item.snapshot_product_name or "").strip()
                    != (item.snapshot_product_name or "").strip()
                ),
                "material_changed": (
                    (previous_item.snapshot_material or "").strip()
                    != (item.snapshot_material or "").strip()
                ),
                "flute_changed": (
                    (previous_item.flute_type or "").strip()
                    != (item.flute_type or "").strip()
                ),
            }
        )
    return candidates


def _validate_previous_batch_choice(
    db: Session,
    *,
    item: OrderItem,
    order: Order,
    previous_order_item_id: int,
) -> OrderItem:
    candidate_ids = {
        int(candidate["order_item_id"])
        for candidate in _previous_batch_candidates(db, item=item, order=order)
    }
    if previous_order_item_id not in candidate_ids:
        raise HTTPException(
            status_code=409,
            detail="所选上一批必须是同客户、同存货编码且尚未送完的较早订单",
        )
    previous_item = db.get(OrderItem, previous_order_item_id)
    assert previous_item is not None
    return previous_item


def _release_requisition_hold(
    db: Session,
    *,
    hold: RequisitionHold,
    user: User,
    source: str,
    note: str,
    expected_version: int | None = None,
) -> bool:
    before = _requisition_hold_audit_state(hold)
    current_version = (
        int(expected_version)
        if expected_version is not None
        else int(hold.version or 0)
    )
    released_at = utc_now_naive()
    result = db.execute(
        update(RequisitionHold)
        .where(
            RequisitionHold.id == hold.id,
            RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
            RequisitionHold.version == current_version,
        )
        .values(
            status=_REQUISITION_HOLD_RELEASED,
            released_by=user.id,
            released_at=released_at,
            release_source=source,
            release_note=note,
            updated_by=user.id,
            version=current_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.flush()
    db.refresh(hold)
    _append_requisition_hold_audit(
        db,
        hold=hold,
        user=user,
        action_code="requisition.hold.release",
        legacy_action="RELEASE_REQUISITION_HOLD",
        result="success",
        source="system" if source.startswith("automatic") else "web",
        description=note,
        transition_source=source,
        before=before,
        after=_requisition_hold_audit_state(hold),
    )
    return True


def _invalidate_requisition_hold(
    db: Session,
    *,
    hold: RequisitionHold,
    user: User,
    source: str,
    note: str,
    expected_version: int | None = None,
) -> bool:
    before = _requisition_hold_audit_state(hold)
    current_version = (
        int(expected_version)
        if expected_version is not None
        else int(hold.version or 0)
    )
    released_at = utc_now_naive()
    result = db.execute(
        update(RequisitionHold)
        .where(
            RequisitionHold.id == hold.id,
            RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
            RequisitionHold.version == current_version,
        )
        .values(
            status=_REQUISITION_HOLD_INVALIDATED,
            released_by=user.id,
            released_at=released_at,
            release_source=source,
            release_note=note,
            updated_by=user.id,
            version=current_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.flush()
    db.refresh(hold)
    _append_requisition_hold_audit(
        db,
        hold=hold,
        user=user,
        action_code="requisition.hold.invalidate",
        legacy_action="INVALIDATE_REQUISITION_HOLD",
        result="success",
        source="system" if source.startswith("automatic") else "web",
        description=note,
        transition_source=source,
        before=before,
        after=_requisition_hold_audit_state(hold),
    )
    return True


def _auto_release_requisition_holds(
    db: Session,
    *,
    user: User,
) -> list[int]:
    allowed = _allowed_customer_ids(user, db)
    query = select(RequisitionHold).where(
        RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
        RequisitionHold.order_item_id.is_not(None),
    )
    if allowed is not None:
        query = query.where(RequisitionHold.customer_id_snapshot.in_(allowed))
    holds = db.scalars(query.order_by(RequisitionHold.id)).all()
    released_ids: list[int] = []
    changed = False
    today = beijing_today()
    for hold in holds:
        assert hold.order_item_id is not None
        try:
            _ensure_requisition_hold_eligible(
                db,
                hold.order_item_id,
                user=user,
                allow_existing_hold=True,
            )
        except HTTPException as error:
            invalidated = _invalidate_requisition_hold(
                db,
                hold=hold,
                user=user,
                source="automatic_eligibility_changed",
                note=f"等候期间业务状态已变化：{error.detail}",
            )
            changed = changed or invalidated
            continue
        source: str | None = None
        note: str | None = None
        if (
            hold.release_mode == "expected_date"
            and hold.expected_requisition_date is not None
            and hold.expected_requisition_date <= today
        ):
            source = "automatic_expected_date"
            note = "预计日期已到，自动恢复待报料"
        elif hold.release_mode == "previous_batch_completed":
            previous_state = _previous_batch_state(db, hold)
            if previous_state["state"] == "completed":
                source = "automatic_previous_batch_dispatched"
                note = "上一批已正式送完，自动恢复待报料"
            elif previous_state["state"] == "abnormal":
                changed = (
                    _append_requisition_hold_anomaly_once(
                        db,
                        hold=hold,
                        user=user,
                        warning=str(previous_state["warning"]),
                    )
                    or changed
                )
        if source and note:
            released = _release_requisition_hold(
                db,
                hold=hold,
                user=user,
                source=source,
                note=note,
            )
            if released:
                released_ids.append(hold.id)
                changed = True
    if changed:
        db.commit()
    return released_ids


def _requisition_hold_requirement_preview(
    db: Session,
    hold: RequisitionHold,
) -> dict:
    """Return a live, non-blocking demand preview for one waiting row.

    A hold stores an immutable order snapshot, while the quantity to requisition
    must always be recalculated from current inventory when it is restored.  A
    broken legacy link must not make the row disappear or make the whole waiting
    list fail, so preview errors are returned as explicit row-level warnings.
    """

    fallback = {
        "live_order_item_exists": False,
        "can_restore_to_pending": False,
        "order_quantity": int(hold.quantity_snapshot or 0),
        "order_unit_label": "只",
        "physical_required_piece_qty": None,
        "physical_requisition_qty": None,
        "component_requirements": [],
        "quantity_changed_since_hold": False,
        "quantity_note": None,
        "requirement_warning": None,
    }
    if hold.order_item_id is None:
        fallback["requirement_warning"] = (
            "订单明细关联已丢失；暂不报料记录仍保留可见，请联系管理员核对，不能静默隐藏"
        )
        return fallback

    item = db.get(OrderItem, hold.order_item_id)
    if item is None:
        fallback["requirement_warning"] = (
            "订单明细已不存在；暂不报料记录仍保留可见，请联系管理员核对，不能静默隐藏"
        )
        return fallback
    product = db.get(Product, item.product_id) if item.product_id else None
    if product is None:
        fallback.update(
            {
                "live_order_item_exists": True,
                "order_quantity": int(item.quantity or 0),
                "requirement_warning": (
                    "订单产品关联异常；暂不报料记录仍保留可见，请先修复产品资料后恢复待报料"
                ),
            }
        )
        return fallback

    try:
        summary = _current_requisition_summary(db, item, product=product)
        if not _bom_pending_component_requirements(db, item):
            summary = _remaining_supplier_requisition_summary(
                db,
                item,
                summary=summary,
            )
    except HTTPException as error:
        detail = str(error.detail or "当前订单资料无法计算报料数量")
        fallback.update(
            {
                "live_order_item_exists": True,
                "order_quantity": int(item.quantity or 0),
                "requirement_warning": (
                    f"报料数量预览失败：{detail}；记录仍保留可见，请修复资料后恢复待报料"
                ),
            }
        )
        return fallback
    except Exception:
        fallback.update(
            {
                "live_order_item_exists": True,
                "order_quantity": int(item.quantity or 0),
                "requirement_warning": (
                    "报料数量预览异常；记录仍保留可见，请刷新或联系管理员核对"
                ),
            }
        )
        return fallback

    components: list[dict] = []
    for requirement in summary.get("component_requirements") or [summary]:
        component_type = str(
            requirement.get("component_type") or "whole"
        ).strip().lower()
        is_base = component_type == "base"
        length_value = (
            item.snapshot_base_report_length_mm
            if is_base
            else item.snapshot_report_length_mm
        )
        width_value = (
            item.snapshot_base_report_width_mm
            if is_base
            else item.snapshot_report_width_mm
        )
        components.append(
            {
                "component_type": component_type,
                "component_label": {
                    "cover": "盖",
                    "base": "底",
                    "whole": "整张",
                }.get(component_type, "组件"),
                "required_piece_qty": int(
                    requirement.get("required_piece_qty") or 0
                ),
                "remaining_required_piece_qty": int(
                    requirement.get("remaining_required_piece_qty") or 0
                ),
                "requisition_qty": int(requirement.get("requisition_qty") or 0),
                "report_length_mm": int(length_value) if length_value else None,
                "report_width_mm": int(width_value) if width_value else None,
            }
        )

    component_types = {row["component_type"] for row in components}
    is_split_box = {"cover", "base"}.issubset(component_types)
    live_quantity = int(item.quantity or 0)
    snapshot_quantity = int(hold.quantity_snapshot or 0)
    quantity_changed = live_quantity != snapshot_quantity
    return {
        "live_order_item_exists": True,
        "can_restore_to_pending": True,
        "order_quantity": live_quantity,
        "order_unit_label": "套" if is_split_box else "只",
        "physical_required_piece_qty": int(
            summary.get("required_piece_qty") or 0
        ),
        "physical_requisition_qty": int(summary.get("requisition_qty") or 0),
        "component_requirements": components,
        "quantity_changed_since_hold": quantity_changed,
        "quantity_note": (
            f"订单数量已由暂缓时的 {snapshot_quantity} 调整为 {live_quantity}，"
            "恢复时将按当前数量和库存重算"
            if quantity_changed
            else "恢复时将按当前订单数量和库存重算"
        ),
        "requirement_warning": None,
    }


def _requisition_hold_dict(
    db: Session,
    hold: RequisitionHold,
) -> dict:
    previous_state = (
        _previous_batch_state(db, hold)
        if hold.release_mode == "previous_batch_completed"
        else None
    )
    created_date = utc_naive_to_beijing_date(hold.created_at)
    previous_warning = previous_state.get("warning") if previous_state else None
    requirement_preview = _requisition_hold_requirement_preview(db, hold)
    requirement_warning = requirement_preview.get("requirement_warning")
    warning = "；".join(
        str(value).strip()
        for value in (previous_warning, requirement_warning)
        if value and str(value).strip()
    ) or None
    release_ready = (
        (
            hold.release_mode == "expected_date"
            and hold.expected_requisition_date is not None
            and hold.expected_requisition_date <= beijing_today()
        )
        or (
            previous_state is not None
            and previous_state.get("state") == "completed"
        )
    )
    condition_status = (
        "anomaly"
        if warning
        else "due"
        if release_ready
        else "waiting"
    )
    return {
        "id": hold.id,
        "order_item_id": hold.order_item_id,
        "order_item_id_snapshot": hold.order_item_id_snapshot,
        "customer_id": hold.customer_id_snapshot,
        "customer_name": hold.customer_name_snapshot,
        "order_number": hold.order_number_snapshot,
        "item_sequence": hold.order_item_sequence_snapshot,
        "product_code": hold.product_code_snapshot,
        "product_name": hold.product_name_snapshot,
        "specification": hold.specification_snapshot,
        "quantity": hold.quantity_snapshot,
        "release_mode": hold.release_mode,
        "previous_order_item_id": hold.previous_order_item_id,
        "previous_order_item_id_snapshot": hold.previous_order_item_id_snapshot,
        "expected_requisition_date": hold.expected_requisition_date,
        "status": hold.status,
        "version": hold.version,
        "previous_batch": previous_state,
        "warning": warning,
        "release_ready": release_ready,
        "is_due": release_ready,
        "is_anomaly": bool(warning),
        "condition_status": condition_status,
        "waiting_days": max((beijing_today() - created_date).days, 0),
        "created_at": (
            utc_naive_to_api(hold.created_at) if hold.created_at is not None else None
        ),
        "updated_at": (
            utc_naive_to_api(hold.updated_at or hold.created_at)
            if (hold.updated_at or hold.created_at) is not None
            else None
        ),
        **requirement_preview,
    }


def _supplier_order_is_visible(
    order: SupplierRequisitionOrder, user: User, db: Session
) -> bool:
    allowed = _allowed_customer_ids(user, db)
    if allowed is None:
        return True
    # Supplier orders only carry a customer name snapshot.  Scope through the
    # linked order item; an unlinked legacy/manual line is deliberately hidden.
    linked_ids = [item.order_item_id for item in order.items if item.order_item_id]
    if not linked_ids or len(linked_ids) != len(order.items):
        return False
    rows = db.execute(
        select(OrderItem.id, Order.customer_id)
        .join(Order, OrderItem.order_id == Order.id)
        .where(OrderItem.id.in_(linked_ids))
    ).all()
    return (
        len(rows) == len(set(linked_ids))
        and {customer_id for _item_id, customer_id in rows}.issubset(allowed)
    )


def _require_supplier_order_customer_access(
    order: SupplierRequisitionOrder, user: User, db: Session
) -> None:
    if not _supplier_order_is_visible(order, user, db):
        raise HTTPException(status_code=403, detail="无客户访问权限")


def _supplier_order_audit_customers(
    db: Session,
    order: SupplierRequisitionOrder,
) -> tuple[list[int], list[str]]:
    """Return stable customer snapshots for one supplier requisition order."""
    linked_ids = sorted(
        {
            int(item.order_item_id)
            for item in order.items
            if item.order_item_id is not None
        }
    )
    if not linked_ids:
        return [], []
    rows = db.execute(
        select(Customer.id, Customer.name)
        .join(Order, Order.customer_id == Customer.id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id.in_(linked_ids))
        .distinct()
        .order_by(Customer.id)
    ).all()
    return (
        [int(customer_id) for customer_id, _name in rows],
        [str(name) for _customer_id, name in rows],
    )


def _apply_supplier_order_scope_ids(query, allowed: set[int] | None):
    if allowed is None:
        return query
    visible_order_ids = (
        select(SupplierRequisitionOrderItem.supplier_order_id)
        .join(OrderItem, OrderItem.id == SupplierRequisitionOrderItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.customer_id.in_(allowed))
    )
    inaccessible_order_ids = (
        select(SupplierRequisitionOrderItem.supplier_order_id)
        .outerjoin(OrderItem, OrderItem.id == SupplierRequisitionOrderItem.order_item_id)
        .outerjoin(Order, Order.id == OrderItem.order_id)
        .where(
            or_(
                SupplierRequisitionOrderItem.order_item_id.is_(None),
                Order.customer_id.is_(None),
                ~Order.customer_id.in_(allowed),
            )
        )
    )
    return query.where(
        SupplierRequisitionOrder.id.in_(visible_order_ids),
        ~SupplierRequisitionOrder.id.in_(inaccessible_order_ids),
    )


def _apply_supplier_order_scope(query, user: User, db: Session):
    return _apply_supplier_order_scope_ids(
        query,
        _allowed_customer_ids(user, db),
    )


def _requisition_is_visible(group: Requisition, user: User, db: Session) -> bool:
    allowed = _allowed_customer_ids(user, db)
    if allowed is None:
        return True
    item_ids = [item.order_item_id for item in group.items]
    if not item_ids:
        return False
    rows = db.execute(
        select(OrderItem.id, Order.customer_id)
        .join(Order, OrderItem.order_id == Order.id)
        .where(OrderItem.id.in_(item_ids))
    ).all()
    return (
        len(rows) == len(set(item_ids))
        and {customer_id for _item_id, customer_id in rows}.issubset(allowed)
    )


def _require_requisition_customer_access(
    group: Requisition, user: User, db: Session
) -> None:
    if not _requisition_is_visible(group, user, db):
        raise HTTPException(status_code=403, detail="无客户访问权限")


def _apply_requisition_scope_ids(query, allowed: set[int] | None):
    if allowed is None:
        return query
    visible_group_ids = (
        select(RequisitionItem.requisition_id)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.customer_id.in_(allowed))
    )
    inaccessible_group_ids = (
        select(RequisitionItem.requisition_id)
        .outerjoin(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .outerjoin(Order, Order.id == OrderItem.order_id)
        .where(
            or_(
                OrderItem.id.is_(None),
                Order.customer_id.is_(None),
                ~Order.customer_id.in_(allowed),
            )
        )
    )
    return query.where(
        Requisition.id.in_(visible_group_ids),
        ~Requisition.id.in_(inaccessible_group_ids),
    )


def _apply_requisition_scope(query, user: User, db: Session):
    return _apply_requisition_scope_ids(
        query,
        _allowed_customer_ids(user, db),
    )


def _item_response(item: OrderItem, db: Session | None = None) -> dict:
    pieces_per_box = _pieces_per_box(item)
    cutting_mode = normalize_cutting_mode(item.special_process)
    requirements = (
        _current_requisition_summary(db, item, cutting_mode=cutting_mode)
        if db is not None
        else None
    )
    finished_reserved_qty = int(
        requirements["finished_inventory_reserved_qty"] if requirements else 0
    )
    production_required_qty = int(
        requirements["production_required_qty"]
        if requirements
        else max(item.quantity - finished_reserved_qty, 0)
    )
    required_piece_qty = int(
        requirements["required_piece_qty"]
        if requirements
        else _required_piece_qty(production_required_qty, pieces_per_box)
    )
    return {
        "item_id": item.id,
        "inventory_deducted_qty": 0,
        "legacy_inventory_deducted_qty": item.inventory_deducted_qty,
        "finished_inventory_reserved_qty": finished_reserved_qty,
        "production_required_qty": production_required_qty,
        "fully_covered_by_finished_inventory": production_required_qty == 0,
        "semi_finished_reserved_piece_qty": int(
            requirements["semi_finished_reserved_piece_qty"]
            if requirements
            else 0
        ),
        "remaining_required_piece_qty": int(
            requirements["remaining_required_piece_qty"]
            if requirements
            else required_piece_qty
        ),
        "requisition_qty": int(requirements["requisition_qty"])
        if requirements
        else item.requisition_qty,
        "requisition_status": item.requisition_status,
        "special_process": item.special_process,
        "cutting_mode": cutting_mode,
        "cutting_factor": _cutting_factor(cutting_mode),
        "pieces_per_box": pieces_per_box,
        "required_piece_qty": required_piece_qty,
        "component_requirements": (
            requirements.get("component_requirements", []) if requirements else []
        ),
        "requisition_spec": item.requisition_spec,
        "cardboard_len": item.cardboard_len,
        "cardboard_width": item.cardboard_width,
        "requisition_date": item.requisition_date,
        "supplier_delivery_time": (
            beijing_naive_to_api(item.supplier_delivery_time)
            if item.supplier_delivery_time
            else None
        ),
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
    total_semi_finished_reserved_piece_qty = 0
    total_remaining_required_piece_qty = 0
    total_requisition_qty = 0
    first_item: OrderItem | None = None
    first_req_item: RequisitionItem | None = None
    first_material: Material | None = None
    for req_item, order_item, order, customer, product in rows:
        display_no = display_order_number(order, registry)
        product_code = req_item.product_code_snapshot or order_item.snapshot_product_code or product.product_code
        product_name = req_item.product_name_snapshot or order_item.snapshot_product_name
        requirements = _current_requisition_requirements(
            db,
            order_item,
            cutting_mode=req_item.special_process,
            pieces_per_box=req_item.pieces_per_box or _pieces_per_box(order_item),
            component_type=_requisition_item_component(req_item),
        )
        if not _requires_supplier_purchase(requirements):
            continue
        if first_item is None:
            first_item = order_item
            first_req_item = req_item
            first_material = db.get(Material, order_item.material_id) if order_item.material_id else None
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        required_piece_qty = int(requirements["required_piece_qty"])
        semi_finished_reserved_piece_qty = int(
            requirements["semi_finished_reserved_piece_qty"]
        )
        remaining_required_piece_qty = int(
            requirements["remaining_required_piece_qty"]
        )
        requisition_qty = int(requirements["requisition_qty"])
        product_codes.append(product_code)
        order_numbers.append(display_no)
        customer_names.append(customer.name)
        product_names.append(product_name)
        total_quantity += int(order_item.quantity or 0)
        total_finished_reserved_qty += finished_reserved_qty
        total_production_required_qty += production_required_qty
        total_required_piece_qty += required_piece_qty
        total_semi_finished_reserved_piece_qty += semi_finished_reserved_piece_qty
        total_remaining_required_piece_qty += remaining_required_piece_qty
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
                "semi_finished_reserved_piece_qty": semi_finished_reserved_piece_qty,
                "remaining_required_piece_qty": remaining_required_piece_qty,
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
        "semi_finished_reserved_piece_qty": total_semi_finished_reserved_piece_qty,
        "remaining_required_piece_qty": total_remaining_required_piece_qty,
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
    active_hold_item_id = db.scalar(
        select(RequisitionHold.order_item_id)
        .where(
            RequisitionHold.order_item_id.in_(member_item_ids),
            RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
        )
        .limit(1)
    )
    if active_hold_item_id is not None:
        raise HTTPException(
            status_code=409,
            detail="所选明细正在等候报料，请先恢复到待报料",
        )
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
        _ensure_order_item_crease_width(item)
    return ordered_rows


def _supplier_requisition_source_key(
    item: OrderItem,
    req_item: RequisitionItem | None = None,
    component_type: str | None = None,
) -> str:
    if req_item is not None:
        return f"requisition_item:{req_item.id}"
    component = str(component_type or "").strip().lower()
    if component in {"cover", "base"}:
        return f"order_item:{item.id}:{component}"
    return f"order_item:{item.id}"


def _source_key_matches_order_item(source_key: str | None, item_id: int) -> bool:
    if not source_key:
        return True
    prefix = f"order_item:{item_id}"
    return source_key == prefix or source_key in {
        f"{prefix}:cover",
        f"{prefix}:base",
    }


def _active_supplier_requisition_facts(
    db: Session,
    *,
    item: OrderItem,
    req_item: RequisitionItem | None = None,
    component_type: str | None = None,
) -> dict[str, object]:
    component = str(component_type or "whole").strip().lower()
    if req_item is None and component == "whole":
        return _active_requisition_facts_by_item_ids(db, [item.id]).get(
            item.id,
            {
                "source_key": f"order_item:{item.id}",
                "quantity": 0,
                "orders": [],
            },
        )
    source_key = _supplier_requisition_source_key(
        item,
        req_item,
        component_type=component,
    )
    rows = db.execute(
        select(SupplierRequisitionOrderItem, SupplierRequisitionOrder, User)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .outerjoin(User, User.id == SupplierRequisitionOrder.created_by)
        .where(
            SupplierRequisitionOrderItem.order_item_id == item.id,
            SupplierRequisitionOrder.status != "voided",
            SupplierRequisitionOrderItem.status == "active",
        )
        .order_by(
            SupplierRequisitionOrder.created_at.asc(),
            SupplierRequisitionOrder.id.asc(),
        )
    ).all()
    facts: list[dict[str, object]] = []
    for order_line, supplier_order, operator in rows:
        if order_line.source_key:
            if order_line.source_key != source_key:
                continue
        elif req_item is not None:
            expected_code = str(req_item.product_code_snapshot or "").strip()
            if expected_code and str(order_line.product_code or "").strip() != expected_code:
                continue
        elif component in {"cover", "base"}:
            expected_suffix = "-盖" if component == "cover" else "-底"
            if not str(order_line.product_name or "").endswith(expected_suffix):
                continue
        facts.append(
            {
                "supplier_order_id": supplier_order.id,
                "supplier_order_number": supplier_order.order_number,
                "supplier_name": supplier_order.supplier_name,
                "requisition_qty": int(order_line.requisition_qty or 0),
                "created_at": (
                    beijing_naive_to_api(supplier_order.created_at)
                    if supplier_order.created_at is not None
                    else None
                ),
                "operator": (
                    operator.display_name
                    or operator.real_name
                    or operator.username
                    if operator is not None
                    else None
                ),
            }
        )
    if req_item is None and component in {"cover", "base"}:
        legacy_rows = db.execute(
            select(RequisitionItem, Requisition, User)
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .outerjoin(User, User.id == Requisition.created_by)
            .where(
                RequisitionItem.order_item_id == item.id,
                func.lower(RequisitionItem.status).notin_(
                    NON_EFFECTIVE_LEGACY_REQUISITION_STATUSES
                ),
                func.lower(Requisition.status).notin_(
                    NON_EFFECTIVE_LEGACY_REQUISITION_STATUSES
                ),
            )
            .order_by(Requisition.created_at.asc(), Requisition.id.asc())
        ).all()
        expected_suffix = "-盖" if component == "cover" else "-底"
        for legacy_line, requisition, operator in legacy_rows:
            if not str(legacy_line.product_name_snapshot or "").endswith(
                expected_suffix
            ):
                continue
            facts.append(
                {
                    "supplier_order_id": None,
                    "supplier_order_number": requisition.requisition_number,
                    "supplier_name": requisition.supplier_name,
                    "requisition_qty": int(legacy_line.requisition_qty or 0),
                    "created_at": (
                        beijing_naive_to_api(requisition.created_at)
                        if requisition.created_at is not None
                        else None
                    ),
                    "operator": (
                        operator.display_name
                        or operator.real_name
                        or operator.username
                        if operator is not None
                        else None
                    ),
                    "source_type": "legacy_material_requisition",
                    "legacy_requisition_id": requisition.id,
                    "legacy_requisition_item_id": legacy_line.id,
                }
            )
    return {
        "source_key": source_key,
        "quantity": sum(int(row["requisition_qty"]) for row in facts),
        "orders": facts,
    }


def _duplicate_requisition_detail(facts: dict[str, object]) -> str:
    orders = list(facts.get("orders") or [])
    if not orders:
        return "当前采购需求已经报完，不能重复普通报料"
    summary = "；".join(
        f"{row['supplier_order_number']} {row['requisition_qty']}张"
        + (f" {str(row['created_at'])[:10]}" if row.get("created_at") else "")
        + (f"（{row['operator']}）" if row.get("operator") else "")
        for row in orders[:3]
    )
    return f"当前采购需求已经报完。已有报料：{summary}。如确需增加，请使用超量报料确认。"


def _active_requisition_facts_by_item_ids(
    db: Session,
    item_ids: list[int],
) -> dict[int, dict[str, object]]:
    clean_ids = sorted({int(item_id) for item_id in item_ids if int(item_id) > 0})
    if not clean_ids:
        return {}
    rows = db.execute(
        select(SupplierRequisitionOrderItem, SupplierRequisitionOrder, User)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .outerjoin(User, User.id == SupplierRequisitionOrder.created_by)
        .where(
            SupplierRequisitionOrderItem.order_item_id.in_(clean_ids),
            SupplierRequisitionOrder.status != "voided",
            SupplierRequisitionOrderItem.status == "active",
        )
        .order_by(
            SupplierRequisitionOrder.created_at.asc(),
            SupplierRequisitionOrder.id.asc(),
        )
    ).all()
    result: dict[int, dict[str, object]] = {
        item_id: {
            "source_key": f"order_item:{item_id}",
            "quantity": 0,
            "orders": [],
        }
        for item_id in clean_ids
    }
    for order_line, supplier_order, operator in rows:
        item_id = int(order_line.order_item_id or 0)
        if item_id not in result:
            continue
        if not _source_key_matches_order_item(order_line.source_key, item_id):
            continue
        fact = {
            "supplier_order_id": supplier_order.id,
            "supplier_order_number": supplier_order.order_number,
            "supplier_name": supplier_order.supplier_name,
            "requisition_qty": int(order_line.requisition_qty or 0),
            "created_at": (
                beijing_naive_to_api(supplier_order.created_at)
                if supplier_order.created_at is not None
                else None
            ),
            "operator": (
                operator.display_name
                or operator.real_name
                or operator.username
                if operator is not None
                else None
            ),
        }
        result[item_id]["quantity"] = int(result[item_id]["quantity"]) + int(
            fact["requisition_qty"]
        )
        item_orders = result[item_id]["orders"]
        if isinstance(item_orders, list):
            item_orders.append(fact)

    legacy_rows = db.execute(
        select(RequisitionItem, Requisition, User)
        .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
        .outerjoin(User, User.id == Requisition.created_by)
        .where(
            RequisitionItem.order_item_id.in_(clean_ids),
            func.lower(RequisitionItem.status).notin_(
                NON_EFFECTIVE_LEGACY_REQUISITION_STATUSES
            ),
            func.lower(Requisition.status).notin_(
                NON_EFFECTIVE_LEGACY_REQUISITION_STATUSES
            ),
        )
        .order_by(Requisition.created_at.asc(), Requisition.id.asc())
    ).all()
    for legacy_line, requisition, operator in legacy_rows:
        item_id = int(legacy_line.order_item_id or 0)
        if item_id not in result:
            continue
        fact = {
            "supplier_order_id": None,
            "supplier_order_number": requisition.requisition_number,
            "supplier_name": requisition.supplier_name,
            "requisition_qty": int(legacy_line.requisition_qty or 0),
            "created_at": (
                beijing_naive_to_api(requisition.created_at)
                if requisition.created_at is not None
                else None
            ),
            "operator": (
                operator.display_name
                or operator.real_name
                or operator.username
                if operator is not None
                else None
            ),
            "source_type": "legacy_material_requisition",
            "legacy_requisition_id": requisition.id,
            "legacy_requisition_item_id": legacy_line.id,
        }
        result[item_id]["quantity"] = int(result[item_id]["quantity"]) + int(
            fact["requisition_qty"]
        )
        item_orders = result[item_id]["orders"]
        if isinstance(item_orders, list):
            item_orders.append(fact)
    return result


def _recommended_supplier_dimensions(
    item: OrderItem,
    product: Product,
    req_item: RequisitionItem | None = None,
    component_type: str | None = None,
) -> tuple[Decimal | None, Decimal | None]:
    if req_item is not None and req_item.cardboard_len and req_item.cardboard_width:
        return Decimal(req_item.cardboard_len), Decimal(req_item.cardboard_width)
    component = str(component_type or "whole").strip().lower()
    report_length = (
        item.snapshot_base_report_length_mm
        if component == "base"
        else item.snapshot_report_length_mm
    )
    report_width = (
        item.snapshot_base_report_width_mm
        if component == "base"
        else item.snapshot_report_width_mm
    )
    suggested_len, suggested_width = _purchase_dimensions(
        report_length,
        report_width,
        DEFAULT_CUTTING_MODE,
    )
    if suggested_len is None or suggested_width is None:
        suggested_len, suggested_width = _suggested_dimensions(product)
    return suggested_len, suggested_width


def _order_item_crease_width_error(item: OrderItem) -> str | None:
    errors = (
        crease_width_error(
            label="订单明细压线",
            crease_type=item.snapshot_crease_type,
            report_width_mm=item.snapshot_report_width_mm,
            left_mm=item.snapshot_crease_left_mm,
            middle_mm=item.snapshot_crease_middle_mm,
            right_mm=item.snapshot_crease_right_mm,
        ),
        crease_width_error(
            label="订单明细底压线",
            crease_type=item.snapshot_base_crease_type,
            report_width_mm=item.snapshot_base_report_width_mm,
            left_mm=item.snapshot_base_crease_left_mm,
            middle_mm=item.snapshot_base_crease_middle_mm,
            right_mm=item.snapshot_base_crease_right_mm,
        ),
    )
    return next((error for error in errors if error), None)


def _ensure_order_item_crease_width(item: OrderItem) -> None:
    error = _order_item_crease_width_error(item)
    if error:
        raise HTTPException(
            status_code=409,
            detail=f"{error}。请先在订单明细中确认单片报料宽和压线尺寸",
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
        .with_for_update()
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
    if item.requisition_status not in {"未报料", "已报料"}:
        raise HTTPException(status_code=409, detail="订单明细当前状态不能继续报料")
    if _active_requisition_hold(db, item.id) is not None:
        raise HTTPException(
            status_code=409,
            detail="订单明细正在等候报料，请先恢复到待报料",
        )
    _ensure_order_item_crease_width(item)
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
            requirements = _current_requisition_summary(
                db,
                item,
                cutting_mode=cutting_mode,
            )
            pieces_per_box = int(requirements["pieces_per_box"])
            finished_reserved_qty = int(
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
            required_piece_qty = int(requirements["required_piece_qty"])
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
                    "semi_finished_reserved_piece_qty": int(
                        requirements["semi_finished_reserved_piece_qty"]
                    ),
                    "remaining_required_piece_qty": int(
                        requirements["remaining_required_piece_qty"]
                    ),
                    "requisition_qty": int(requirements["requisition_qty"]),
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
                component_type=_requisition_item_component(req_item),
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
                    "semi_finished_reserved_piece_qty": int(
                        requirements["semi_finished_reserved_piece_qty"]
                    ),
                    "remaining_required_piece_qty": int(
                        requirements["remaining_required_piece_qty"]
                    ),
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
    component_type = str(entry.get("component_type") or "whole").strip().lower()
    component_suffix = (
        "-盖" if component_type == "cover" else "-底" if component_type == "base" else ""
    )
    material = db_material = entry.get("material")
    if material is None and order_item.material_id:
        db_material = None
    return {
        "source_type": entry["source_type"],
        "component_type": component_type,
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
            else f"{order_item.snapshot_product_name}{component_suffix}"
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
        "semi_finished_reserved_piece_qty": entry.get(
            "semi_finished_reserved_piece_qty", 0
        ),
        "remaining_required_piece_qty": entry.get(
            "remaining_required_piece_qty", entry["required_piece_qty"]
        ),
        "requisition_qty": entry["requisition_qty"],
        "theoretical_requisition_qty": entry.get(
            "theoretical_requisition_qty", entry["requisition_qty"]
        ),
        "already_requisitioned_qty": entry.get("already_requisitioned_qty", 0),
        "remaining_requisition_qty": entry.get(
            "remaining_requisition_qty", entry["requisition_qty"]
        ),
        "existing_supplier_orders": entry.get("existing_supplier_orders", []),
        "recommended_report_length_mm": entry.get(
            "recommended_report_length_mm"
        ),
        "recommended_report_width_mm": entry.get(
            "recommended_report_width_mm"
        ),
        "remark": entry["remark"] or "",
        "late_finished_inventory": entry.get(
            "late_finished_inventory",
            {
                "available_quantity": 0,
                "reservable_quantity": 0,
                "remaining_order_quantity": entry["production_required_qty"],
                "can_auto_reserve": False,
                "blocked_reason": None,
                "locations": [],
                "lots": [],
            },
        ),
        "late_semi_inventory_options": entry.get(
            "late_semi_inventory_options", []
        ),
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
    component_type = str(entry.get("component_type") or "whole").strip().lower()
    crease_type, crease_left, crease_middle, crease_right = _component_crease(
        order_item,
        component_type,
    )
    material: Material | None = entry.get("material")
    material_id = order_item.material_id or (material.id if material else None)
    material_code = material.code if material else order_item.snapshot_material
    layer_count = order_item.layer_count or (material.layer_count if material else None)
    flute_type = _clean_supplier_flute(order_item.flute_type)
    clean_material_code = _clean_supplier_material_code(material_code, layer_count)
    return {
        "component_type": component_type,
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
        "crease_type": crease_type,
        "crease_left_mm": crease_left,
        "crease_middle_mm": crease_middle,
        "crease_right_mm": crease_right,
        "cutting_mode": entry.get("cutting_mode") or DEFAULT_CUTTING_MODE,
        "remark": entry.get("remark") or "",
    }


def _purchase_line_key(supplier_name: str | None, spec: dict) -> str:
    key_payload = {
        "supplier_name": (supplier_name or "").strip(),
        # A3 盖片与底片是两条独立物理来源；即使采购规格偶然相同，
        # 也不能在报料单重建时合并成一条虚假数量。
        "component_type": spec.get("component_type") or "whole",
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
                "semi_finished_reserved_piece_qty": 0,
                "remaining_required_piece_qty": 0,
                "requisition_qty": 0,
                "theoretical_requisition_qty": 0,
                "already_requisitioned_qty": 0,
                "remaining_requisition_qty": 0,
                "existing_supplier_orders": [],
                "recommended_report_length_mm": entry.get(
                    "recommended_report_length_mm"
                ),
                "recommended_report_width_mm": entry.get(
                    "recommended_report_width_mm"
                ),
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
        line["semi_finished_reserved_piece_qty"] += int(
            entry.get("semi_finished_reserved_piece_qty") or 0
        )
        line["remaining_required_piece_qty"] += int(
            entry.get("remaining_required_piece_qty") or 0
        )
        line["requisition_qty"] += int(entry.get("requisition_qty") or 0)
        line["theoretical_requisition_qty"] += int(
            entry.get("theoretical_requisition_qty") or 0
        )
        line["already_requisitioned_qty"] += int(
            entry.get("already_requisitioned_qty") or 0
        )
        line["remaining_requisition_qty"] += int(
            entry.get("remaining_requisition_qty") or 0
        )
        line["existing_supplier_orders"].extend(
            entry.get("existing_supplier_orders") or []
        )
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
    user: User,
) -> dict:
    registry = build_display_registry(db)
    grouped: dict[str, list[dict]] = {}
    seen_source_keys: set[tuple[int, str]] = set()
    seen_group_ids: set[int] = set()

    def add_preview(supplier_name: str, entry: dict) -> None:
        source_key = (
            int(entry["order_item"].id),
            str(entry.get("component_type") or "whole").strip().lower(),
        )
        if source_key in seen_source_keys:
            raise HTTPException(status_code=409, detail="同一订单物理材料不能重复加入报料草稿")
        seen_source_keys.add(source_key)
        entry["display_registry"] = registry
        entry["late_finished_inventory"] = _late_finished_inventory_preview(
            db,
            item=entry["order_item"],
            order=entry["order"],
            product=entry["product"],
        )
        entry["late_semi_inventory_options"] = _late_semi_inventory_options(
            db, entry
        )
        grouped.setdefault(supplier_name, []).append(entry)

    for selection in payload.selections:
        if selection.type == "order_item":
            if not selection.order_item_id:
                raise HTTPException(status_code=400, detail="普通待报料行缺少 order_item_id")
            item, order, customer, product = _ensure_pending_order_item_for_supplier_order(
                db, selection.order_item_id
            )
            _require_order_item_customer_access(db, item, user)
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
            requirements = _current_requisition_summary(
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
            material = db.get(Material, item.material_id) if item.material_id else None
            component_requirements = list(
                requirements.get("component_requirements") or [requirements]
            )
            added_component = False
            duplicate_facts: list[dict] = []
            for component_requirements_row in component_requirements:
                component_type = str(
                    component_requirements_row.get("component_type") or "whole"
                ).strip().lower()
                if not _requires_supplier_purchase(component_requirements_row):
                    continue
                active_requisition = _active_supplier_requisition_facts(
                    db,
                    item=item,
                    component_type=component_type,
                )
                theoretical_requisition_qty = int(
                    component_requirements_row["requisition_qty"]
                )
                remaining_requisition_qty = max(
                    theoretical_requisition_qty
                    - int(active_requisition["quantity"]),
                    0,
                )
                if remaining_requisition_qty <= 0:
                    duplicate_facts.extend(active_requisition.get("orders") or [])
                    continue
                is_base = component_type == "base"
                component_len = _entry_decimal(
                    item.snapshot_base_report_length_mm if is_base else cardboard_len,
                    item.snapshot_base_report_length_mm if is_base else cardboard_len,
                    "底片报料长" if is_base else "报料长",
                )
                component_width = _entry_decimal(
                    item.snapshot_base_report_width_mm if is_base else cardboard_width,
                    item.snapshot_base_report_width_mm if is_base else cardboard_width,
                    "底片报料宽" if is_base else "报料宽",
                )
                recommended_len, recommended_width = _recommended_supplier_dimensions(
                    item,
                    product,
                    component_type=component_type,
                )
                add_preview(
                    supplier_name,
                    {
                        "source_type": "order_item",
                        "component_type": component_type,
                        "group": None,
                        "req_item": None,
                        "order_item": item,
                        "order": order,
                        "customer": customer,
                        "product": product,
                        "material": material,
                        "cardboard_len": component_len,
                        "cardboard_width": component_width,
                        "cutting_mode": cutting_mode,
                        "remark": (
                            selection.remark
                            or (
                                item.snapshot_base_report_notes
                                if is_base
                                else item.snapshot_report_notes
                            )
                            or item.requisition_remark
                            or ""
                        ).strip()
                        or None,
                        "inventory_deducted_qty": int(
                            component_requirements_row[
                                "finished_inventory_reserved_qty"
                            ]
                        ),
                        "pieces_per_box": int(
                            component_requirements_row["pieces_per_box"]
                        ),
                        "production_required_qty": int(
                            component_requirements_row["production_required_qty"]
                        ),
                        "required_piece_qty": int(
                            component_requirements_row["required_piece_qty"]
                        ),
                        "semi_finished_reserved_piece_qty": int(
                            component_requirements_row[
                                "semi_finished_reserved_piece_qty"
                            ]
                        ),
                        "remaining_required_piece_qty": int(
                            component_requirements_row[
                                "remaining_required_piece_qty"
                            ]
                        ),
                        "requisition_qty": remaining_requisition_qty,
                        "theoretical_requisition_qty": theoretical_requisition_qty,
                        "already_requisitioned_qty": int(
                            active_requisition["quantity"]
                        ),
                        "remaining_requisition_qty": remaining_requisition_qty,
                        "existing_supplier_orders": active_requisition["orders"],
                        "recommended_report_length_mm": recommended_len,
                        "recommended_report_width_mm": recommended_width,
                    },
                )
                added_component = True
            if not added_component:
                if duplicate_facts:
                    raise HTTPException(
                        status_code=409,
                        detail=_duplicate_requisition_detail(
                            {"orders": duplicate_facts}
                        ),
                    )
                raise HTTPException(
                    status_code=409,
                    detail="该订单明细已由半成品库存全额抵扣，无需生成供应商报料单，请刷新待报料列表。",
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
        _require_requisition_customer_access(group, user, db)
        if group.status != "merged_pending":
            raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
        rows = _merge_group_rows(db, group.id)
        if not rows:
            raise HTTPException(status_code=400, detail="合并组没有来源明细")
        supplier_name = (selection.supplier_name or group.supplier_name or "").strip()
        preview_count_before_group = len(seen_source_keys)
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
                component_type=_requisition_item_component(req_item),
            )
            if bool(requirements["fully_covered_by_finished_inventory"]):
                continue
            if not _requires_supplier_purchase(requirements):
                continue
            active_requisition = _active_supplier_requisition_facts(
                db,
                item=order_item,
                req_item=req_item,
            )
            theoretical_requisition_qty = int(requirements["requisition_qty"])
            remaining_requisition_qty = max(
                theoretical_requisition_qty
                - int(active_requisition["quantity"]),
                0,
            )
            if remaining_requisition_qty <= 0:
                continue
            recommended_len, recommended_width = _recommended_supplier_dimensions(
                order_item,
                product,
                req_item,
            )
            add_preview(
                supplier_name,
                {
                    "source_type": "merge_group_item",
                    "component_type": _requisition_item_component(req_item),
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
                    "semi_finished_reserved_piece_qty": int(
                        requirements["semi_finished_reserved_piece_qty"]
                    ),
                    "remaining_required_piece_qty": int(
                        requirements["remaining_required_piece_qty"]
                    ),
                    "requisition_qty": remaining_requisition_qty,
                    "theoretical_requisition_qty": theoretical_requisition_qty,
                    "already_requisitioned_qty": int(
                        active_requisition["quantity"]
                    ),
                    "remaining_requisition_qty": remaining_requisition_qty,
                    "existing_supplier_orders": active_requisition["orders"],
                    "recommended_report_length_mm": recommended_len,
                    "recommended_report_width_mm": recommended_width,
                },
            )
        if len(seen_source_keys) == preview_count_before_group:
            raise HTTPException(
                status_code=409,
                detail="该合并组来源明细已由库存全额抵扣，无需生成供应商报料单，请刷新待报料列表。",
            )

    supplier_groups = []
    for supplier_name, entries in grouped.items():
        lines = _aggregate_entries_to_purchase_lines(supplier_name, entries)
        supplier_groups.append(
            {
                "supplier_name": supplier_name,
                "request_key": uuid4().hex,
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
    seen_suppliers: set[str] = set()

    for group_payload in payload.supplier_groups:
        supplier_name = (group_payload.supplier_name or "").strip()
        if not supplier_name:
            raise HTTPException(status_code=400, detail="每个供应商组必须选择供应商")
        if supplier_name in seen_suppliers:
            raise HTTPException(status_code=400, detail="同一供应商只能保留一个报料组")
        seen_suppliers.add(supplier_name)
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

            requirements = _current_requisition_requirements(
                db,
                item,
                cutting_mode=draft_item.cutting_mode,
                pieces_per_box=(
                    req_item.pieces_per_box
                    if req_item is not None and req_item.pieces_per_box
                    else _pieces_per_box(item)
                ),
                component_type=_requisition_item_component(req_item),
            )
            actual_finished_reserved = int(
                requirements["finished_inventory_reserved_qty"]
            )
            if draft_item.inventory_deducted_qty not in {
                0,
                actual_finished_reserved,
            }:
                raise HTTPException(
                    status_code=400,
                    detail="成品库存抵扣只能来自当前有效的真实库存预占，请刷新后重试",
                )
            production_required_qty = int(requirements["production_required_qty"])
            if production_required_qty <= 0:
                raise HTTPException(status_code=409, detail="该订单明细已由成品库存全额抵扣，无需报料")
            if not _requires_supplier_purchase(requirements):
                raise HTTPException(
                    status_code=409,
                    detail="该订单明细已由半成品库存全额抵扣，无需报料，请刷新待报料列表。",
                )
            pieces_per_box = int(requirements["pieces_per_box"])
            required_piece_qty = int(requirements["required_piece_qty"])
            requisition_qty = int(requirements["requisition_qty"])
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
                "inventory_deducted_qty": actual_finished_reserved,
                "pieces_per_box": pieces_per_box,
                "production_required_qty": production_required_qty,
                "required_piece_qty": required_piece_qty,
                "semi_finished_reserved_piece_qty": int(
                    requirements["semi_finished_reserved_piece_qty"]
                ),
                "remaining_required_piece_qty": int(
                    requirements["remaining_required_piece_qty"]
                ),
                "requisition_qty": requisition_qty,
            }
            if req_item is not None:
                req_item.cardboard_len = draft_item.report_length_mm
                req_item.cardboard_width = draft_item.report_width_mm
                req_item.special_process = draft_item.cutting_mode
                req_item.requisition_qty = requisition_qty
                req_item.required_piece_qty = required_piece_qty
                req_item.remark = entry["remark"]
            grouped.setdefault(supplier_name, []).append(entry)

    return grouped, list(touched_groups_by_id.values())


def _draft_group_entries_by_purchase_lines(
    db: Session,
    payload: PendingSupplierOrderFinalizePayload,
    user: User,
) -> tuple[dict[str, list[dict]], list[Requisition]]:
    grouped: dict[str, list[dict]] = {}
    touched_groups_by_id: dict[int, Requisition] = {}
    seen_components_by_item: dict[int, set[str]] = {}
    seen_suppliers: set[str] = set()

    for group_payload in payload.supplier_groups:
        supplier_name = (group_payload.supplier_name or "").strip()
        if not supplier_name:
            raise HTTPException(status_code=400, detail="每个供应商组必须选择供应商")
        if supplier_name in seen_suppliers:
            raise HTTPException(status_code=400, detail="同一供应商只能保留一个报料组")
        seen_suppliers.add(supplier_name)
        draft_lines = _draft_lines_from_group(group_payload)
        if not draft_lines:
            raise HTTPException(status_code=400, detail="每个供应商组必须至少包含一条采购规格行")

        for draft_line in draft_lines:
            source_refs: list[dict] = []
            for source_payload in draft_line.source_items:
                item, order, customer, product = _ensure_pending_order_item_for_supplier_order(
                    db, source_payload.order_item_id
                )
                _require_order_item_customer_access(db, item, user)
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
                component_type = (
                    _requisition_item_component(req_item)
                    if req_item is not None
                    else str(source_payload.component_type or "whole").strip().lower()
                )
                if (
                    req_item is None
                    and component_type == "whole"
                    and _is_telescoping_lid_box(product.box_style)
                    and item.snapshot_base_report_length_mm
                    and item.snapshot_base_report_width_mm
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="天地盖报料草稿缺少盖片/底片身份，请刷新草稿后重试",
                    )
                seen_components = seen_components_by_item.setdefault(item.id, set())
                if component_type in seen_components:
                    raise HTTPException(status_code=409, detail="同一订单物理材料不能重复生成供应商报料单")
                if seen_components and (
                    component_type == "whole" or "whole" in seen_components
                ):
                    raise HTTPException(status_code=409, detail="同一订单不能同时按整单和组件报料")
                seen_components.add(component_type)
                source_refs.append(
                    {
                        "source_payload": source_payload,
                        "item": item,
                        "order": order,
                        "customer": customer,
                        "product": product,
                        "req_item": req_item,
                        "merge_group": merge_group,
                        "component_type": component_type,
                    }
                )

            for ref in source_refs:
                _require_late_finished_inventory_resolved(
                    db,
                    item=ref["item"],
                    order=ref["order"],
                    product=ref["product"],
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
                    component_type=ref["component_type"],
                )
                for ref in source_refs
            ]
            active_requisitions = [
                _active_supplier_requisition_facts(
                    db,
                    item=ref["item"],
                    req_item=ref["req_item"],
                    component_type=ref["component_type"],
                )
                for ref in source_refs
            ]
            if not group_payload.request_key and any(
                int(active["quantity"]) > 0 for active in active_requisitions
            ):
                combined_orders = [
                    row
                    for active in active_requisitions
                    for row in list(active.get("orders") or [])
                ]
                raise HTTPException(
                    status_code=409,
                    detail=(
                        _duplicate_requisition_detail({"orders": combined_orders})
                        + " 旧页面不能直接追加，请刷新后从当前报料草稿进入。"
                    ),
                )
            remaining_requisition_quantities = [
                max(
                    int(requirements["requisition_qty"])
                    - int(active["quantity"]),
                    0,
                )
                for requirements, active in zip(
                    current_requirements,
                    active_requisitions,
                )
            ]
            remaining_line_total = sum(remaining_requisition_quantities)
            if remaining_line_total <= 0:
                combined_orders = [
                    row
                    for active in active_requisitions
                    for row in list(active.get("orders") or [])
                ]
                raise HTTPException(
                    status_code=409,
                    detail=_duplicate_requisition_detail(
                        {"orders": combined_orders}
                    ),
                )
            requested_line_total = int(draft_line.requisition_qty or 0)
            if requested_line_total <= 0:
                raise HTTPException(status_code=400, detail="本次报料张数必须大于 0")
            is_over_quantity = requested_line_total > remaining_line_total
            submitted_source_quantities = [
                (
                    int(ref["source_payload"].requisition_qty)
                    if ref["source_payload"].requisition_qty is not None
                    else None
                )
                for ref in source_refs
            ]
            stale_source_quantities = any(
                submitted is not None and submitted != remaining
                for submitted, remaining in zip(
                    submitted_source_quantities,
                    remaining_requisition_quantities,
                )
            )
            submitted_source_total = sum(
                submitted or 0 for submitted in submitted_source_quantities
            )
            if (
                is_over_quantity
                and not draft_line.quantity_override_acknowledged
                and stale_source_quantities
                and submitted_source_total == requested_line_total
            ):
                # A stale page can carry both the old line total and the old
                # per-source totals after a finished-inventory reservation or
                # double-splice recomputation. Those hidden source values are
                # not an operator overage decision, so normalize them to the
                # current server-derived demand before applying the overage
                # gate. A deliberate line-only increase still requires the
                # explicit admin acknowledgement below.
                requested_line_total = remaining_line_total
                is_over_quantity = False
            if is_over_quantity and user.role not in {"admin", "boss"}:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"本次报料 {requested_line_total} 张，超过剩余待报 "
                        f"{remaining_line_total} 张；只有管理员可确认超量报料"
                    ),
                )
            if is_over_quantity and not draft_line.quantity_override_acknowledged:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"本次报料 {requested_line_total} 张，超过剩余待报 "
                        f"{remaining_line_total} 张。请勾选“确认超量报料”后再保存"
                    ),
                )

            recommended_dimensions = [
                _recommended_supplier_dimensions(
                    ref["item"],
                    ref["product"],
                    ref["req_item"],
                    component_type=ref["component_type"],
                )
                for ref in source_refs
            ]
            swapped_sources = [
                (ref, expected_len, expected_width)
                for ref, (expected_len, expected_width) in zip(
                    source_refs,
                    recommended_dimensions,
                )
                if expected_len is not None
                and expected_width is not None
                and expected_len != expected_width
                and Decimal(draft_line.report_length_mm) == Decimal(expected_width)
                and Decimal(draft_line.report_width_mm) == Decimal(expected_len)
            ]
            if swapped_sources and user.role not in {"admin", "boss"}:
                expected_len, expected_width = swapped_sources[0][1:]
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "疑似长宽颠倒："
                        f"系统推荐 {_plain(expected_len)}×{_plain(expected_width)}，"
                        f"人工输入 {_plain(draft_line.report_length_mm)}×"
                        f"{_plain(draft_line.report_width_mm)}。请修改后再保存"
                    ),
                )
            if swapped_sources and not draft_line.dimension_override_acknowledged:
                expected_len, expected_width = swapped_sources[0][1:]
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "疑似长宽颠倒："
                        f"系统推荐 {_plain(expected_len)}×{_plain(expected_width)}，"
                        f"人工输入 {_plain(draft_line.report_length_mm)}×"
                        f"{_plain(draft_line.report_width_mm)}。"
                        "请勾选“按人工尺寸继续”后再保存"
                    ),
                )

            requisition_allocations = _allocate_integer_total(
                requested_line_total,
                remaining_requisition_quantities,
            )
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

            for ref, requirements, active_requisition, remaining_before, allocated_qty, expected_dimensions in zip(
                source_refs,
                current_requirements,
                active_requisitions,
                remaining_requisition_quantities,
                requisition_allocations,
                recommended_dimensions,
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
                requisition_qty = int(allocated_qty)
                if requisition_qty <= 0:
                    continue

                if inventory_deducted_qty < 0:
                    raise HTTPException(status_code=400, detail="成品库存抵扣不能小于 0")
                if inventory_deducted_qty > item.quantity:
                    raise HTTPException(status_code=400, detail="成品库存抵扣不能大于订单数量")
                production_required_qty = int(
                    requirements["production_required_qty"]
                )
                if production_required_qty <= 0:
                    raise HTTPException(
                        status_code=409,
                        detail="该订单明细已由成品库存全额抵扣，无需生成供应商报料单",
                    )
                if not _requires_supplier_purchase(requirements):
                    raise HTTPException(
                        status_code=409,
                        detail="该订单明细已由半成品库存全额抵扣，无需生成供应商报料单，请刷新待报料列表。",
                    )

                pieces_per_box = int(requirements["pieces_per_box"])
                required_piece_qty = int(requirements["required_piece_qty"])
                material = db.get(Material, item.material_id) if item.material_id else None
                entry = {
                    "source_type": source_payload.source_type,
                    "component_type": ref["component_type"],
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
                    "semi_finished_reserved_piece_qty": int(
                        requirements["semi_finished_reserved_piece_qty"]
                    ),
                    "remaining_required_piece_qty": int(
                        requirements["remaining_required_piece_qty"]
                    ),
                    "requisition_qty": requisition_qty,
                    "theoretical_requisition_qty": int(
                        requirements["requisition_qty"]
                    ),
                    "already_requisitioned_qty": int(
                        active_requisition["quantity"]
                    ),
                    "remaining_requisition_qty": int(remaining_before),
                    "remaining_after_requisition_qty": max(
                        int(remaining_before) - requisition_qty,
                        0,
                    ),
                    "source_key": active_requisition["source_key"],
                    "request_key": group_payload.request_key,
                    "recommended_report_length_mm": expected_dimensions[0],
                    "recommended_report_width_mm": expected_dimensions[1],
                    "dimension_override": bool(swapped_sources),
                    "quantity_override": is_over_quantity,
                }
                if req_item is not None:
                    req_item.cardboard_len = draft_line.report_length_mm
                    req_item.cardboard_width = draft_line.report_width_mm
                    req_item.special_process = draft_line.cutting_mode
                    req_item.requisition_qty = int(
                        active_requisition["quantity"]
                    ) + requisition_qty
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
    supplier_name = _require_active_supplier(db, supplier_name)
    first = entries[0]
    first_item: OrderItem = first["order_item"]
    material = db.get(Material, first_item.material_id) if first_item.material_id else None
    _require_active_material_supplier(db, material)
    layer_count = material.layer_count if material else first_item.layer_count
    flute_type, flute_error = _business_flute_error(
        layer_count,
        first_item.flute_type,
    )
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)
    order = SupplierRequisitionOrder(
        order_number=_supplier_order_number(db),
        request_key=first.get("request_key"),
        supplier_name=supplier_name,
        material_id=first_item.material_id,
        layer_count=layer_count,
        flute_type=flute_type,
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
    requisition_date = beijing_today()
    entries_by_order_item: dict[int, list[dict]] = {}
    for entry in entries:
        order_item: OrderItem = entry["order_item"]
        req_item: RequisitionItem | None = entry["req_item"]
        component_type = str(entry.get("component_type") or "whole").strip().lower()
        component_suffix = (
            "-盖"
            if component_type == "cover"
            else "-底" if component_type == "base" else ""
        )
        db.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=order.id,
                order_item_id=order_item.id,
                **_supplier_item_snapshot_values(
                    db,
                    order_item,
                    fallback_material_id=order.material_id,
                    fallback_supplier_name=order.supplier_name,
                    fallback_layer_count=order.layer_count,
                    fallback_flute_type=order.flute_type,
                ),
                order_number=order_item.item_order_number,
                product_code=(
                    req_item.product_code_snapshot
                    if req_item is not None
                    else order_item.snapshot_product_code or entry["product"].product_code
                ),
                product_name=(
                    req_item.product_name_snapshot
                    if req_item is not None
                    else f"{order_item.snapshot_product_name}{component_suffix}"
                ),
                source_key=entry.get("source_key"),
                report_length_mm=int(entry["cardboard_len"]),
                report_width_mm=int(entry["cardboard_width"]),
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
        entries_by_order_item.setdefault(order_item.id, []).append(entry)
        if req_item is not None:
            req_item.status = (
                "supplier_requisition_created"
                if int(entry.get("remaining_after_requisition_qty") or 0) <= 0
                else "merged_pending"
            )
    db.flush()
    active_by_item = _active_requisition_facts_by_item_ids(
        db,
        list(entries_by_order_item),
    )
    for order_item_id, item_entries in entries_by_order_item.items():
        order_item: OrderItem = item_entries[0]["order_item"]
        first_entry = next(
            (
                row
                for row in item_entries
                if str(row.get("component_type") or "whole").strip().lower()
                in {"whole", "cover"}
            ),
            item_entries[0],
        )
        component_types = {
            str(row.get("component_type") or "whole").strip().lower()
            for row in item_entries
        }
        # The legacy free-entry field stays zero. Real deductions are recorded
        # by InventoryReservation and copied only to supplier-order snapshots.
        order_item.inventory_deducted_qty = 0
        order_item.requisition_status = "已报料"
        order_item.requisition_qty = int(
            active_by_item.get(order_item_id, {}).get("quantity") or 0
        )
        order_item.special_process = first_entry["cutting_mode"]
        order_item.cardboard_len = first_entry["cardboard_len"]
        order_item.cardboard_width = first_entry["cardboard_width"]
        if component_types & {"cover", "base"}:
            order_item.requisition_spec = "；".join(
                part
                for part in (
                    (
                        f"盖:{_plain(order_item.snapshot_report_length_mm)}×"
                        f"{_plain(order_item.snapshot_report_width_mm)}"
                        if order_item.snapshot_report_length_mm
                        and order_item.snapshot_report_width_mm
                        else None
                    ),
                    (
                        f"底:{_plain(order_item.snapshot_base_report_length_mm)}×"
                        f"{_plain(order_item.snapshot_base_report_width_mm)}"
                        if order_item.snapshot_base_report_length_mm
                        and order_item.snapshot_base_report_width_mm
                        else None
                    ),
                )
                if part
            )
        else:
            order_item.requisition_spec = (
                f"{_plain(first_entry['cardboard_len'])}×"
                f"{_plain(first_entry['cardboard_width'])}"
            )
        order_item.requisition_date = requisition_date
        order_item.supplier_order_number = order.order_number
        order_item.requisition_remark = first_entry["remark"]
    return order


@router.post("/order-entry/hold-preview")
def preview_order_entry_holds(
    payload: OrderEntryHoldPreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    items: list[dict] = []
    for index, line in enumerate(payload.lines, start=1):
        code = (line.product_code or "").strip()
        if line.finished_covered_quantity >= line.quantity:
            items.append({"client_line_id": line.client_line_id, "status": "covered", "candidates": [], "warnings": [], "selected_previous_order_item_id": None})
            continue
        if not code:
            items.append({"client_line_id": line.client_line_id, "status": "blocked", "candidates": [], "warnings": [f"第{index}条明细缺少存货编码，不能等待上一批"], "selected_previous_order_item_id": None})
            continue
        rows = db.execute(select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id).where(
            Order.customer_id == payload.customer_id,
            func.trim(OrderItem.snapshot_product_code) == code,
            OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.is_force_closed.is_(False),
            Order.status.notin_(_REQUISITION_HOLD_ABNORMAL_ORDER_STATUSES),
        ).order_by(OrderItem.created_at.desc(), OrderItem.id.desc()).limit(20)).all()
        candidates = [{"order_item_id": item.id, "order_number": order.order_number,
                       "item_order_number": item.item_order_number, "remaining_quantity": max(int(item.quantity)-int(item.delivered_quantity), 0),
                       "specification": item.snapshot_spec, "material": item.snapshot_material, "flute_type": item.flute_type}
                      for item, order in rows]
        warnings: list[str] = []
        selected = None
        if len(candidates) == 1:
            candidate = candidates[0]
            mismatch = any((str(candidate[key] or "").strip() != str(value or "").strip()) for key, value in (("specification", line.specification), ("material", line.material), ("flute_type", line.flute_type)))
            if mismatch:
                warnings.append("上一批规格、材质或楞型快照不一致，不能自动等待")
            else:
                selected = candidate["order_item_id"]
        elif len(candidates) > 1:
            latest_candidate = candidates[0]
            latest_hold_id = db.scalar(
                select(RequisitionHold.id)
                .where(
                    RequisitionHold.order_item_id
                    == int(latest_candidate["order_item_id"]),
                    RequisitionHold.status == "active",
                )
                .limit(1)
            )
            latest_mismatch = any(
                str(latest_candidate[key] or "").strip()
                != str(value or "").strip()
                for key, value in (
                    ("specification", line.specification),
                    ("material", line.material),
                    ("flute_type", line.flute_type),
                )
            )
            if latest_hold_id is not None and not latest_mismatch:
                selected = int(latest_candidate["order_item_id"])
                warnings.append("已按连续批次自动衔接最新等候订单")
            else:
                warnings.append("存在多个未送完的同款上一批，请选择要等待的批次")
        items.append({"client_line_id": line.client_line_id, "status": "hold" if selected else ("normal" if not candidates else "select_required"), "candidates": candidates, "warnings": warnings, "selected_previous_order_item_id": selected})
    return {"items": items}


@router.get("/pending/{item_id}/previous-batch-candidates")
def requisition_hold_previous_batch_candidates(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    item, order, customer, _product = _ensure_requisition_hold_eligible(
        db,
        item_id,
        user=user,
        allow_existing_hold=True,
    )
    candidates = _previous_batch_candidates(db, item=item, order=order)
    return {
        "order_item_id": item.id,
        "customer_id": customer.id,
        "product_code": item.snapshot_product_code,
        "items": candidates,
        "total": len(candidates),
        "recommended_order_item_id": (
            int(candidates[0]["order_item_id"]) if candidates else None
        ),
    }


@router.post("/holds", status_code=status.HTTP_201_CREATED)
def create_requisition_holds(
    payload: RequisitionHoldBatchCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    results: list[dict] = []
    seen_item_ids: set[int] = set()
    for selection in payload.items:
        if selection.order_item_id in seen_item_ids:
            results.append(
                {
                    "order_item_id": selection.order_item_id,
                    "ok": False,
                    "message": "同一批次中不能重复选择同一订单明细",
                }
            )
            continue
        seen_item_ids.add(selection.order_item_id)
        try:
            item, order, customer, _product = _ensure_requisition_hold_eligible(
                db,
                selection.order_item_id,
                user=user,
                lock=True,
            )
            previous_item: OrderItem | None = None
            if selection.release_mode == "previous_batch_completed":
                assert selection.previous_order_item_id is not None
                previous_item = _validate_previous_batch_choice(
                    db,
                    item=item,
                    order=order,
                    previous_order_item_id=selection.previous_order_item_id,
                )
            else:
                assert selection.expected_requisition_date is not None
                if selection.expected_requisition_date <= beijing_today():
                    raise HTTPException(
                        status_code=409,
                        detail="预计恢复报料日期必须晚于今天",
                    )
            hold = RequisitionHold(
                order_item_id=item.id,
                order_item_id_snapshot=item.id,
                customer_id_snapshot=customer.id,
                customer_name_snapshot=customer.name,
                order_number_snapshot=order.order_number,
                order_item_sequence_snapshot=item.item_sequence,
                product_code_snapshot=item.snapshot_product_code,
                product_name_snapshot=item.snapshot_product_name,
                specification_snapshot=item.snapshot_spec,
                quantity_snapshot=int(item.quantity or 0),
                release_mode=selection.release_mode,
                previous_order_item_id=previous_item.id if previous_item else None,
                previous_order_item_id_snapshot=(
                    previous_item.id if previous_item else None
                ),
                expected_requisition_date=selection.expected_requisition_date,
                status=_REQUISITION_HOLD_ACTIVE,
                created_by=user.id,
                updated_by=user.id,
            )
            db.add(hold)
            db.flush()
            _append_requisition_hold_audit(
                db,
                hold=hold,
                user=user,
                action_code="requisition.hold.create",
                legacy_action="CREATE_REQUISITION_HOLD",
                result="success",
                source="web",
                description="订单明细移入等候报料",
                transition_source="manual_create",
                before=None,
                after=_requisition_hold_audit_state(hold),
            )
            db.commit()
            results.append(
                {
                    "order_item_id": item.id,
                    "ok": True,
                    "hold": _requisition_hold_dict(db, hold),
                }
            )
        except HTTPException as error:
            db.rollback()
            results.append(
                {
                    "order_item_id": selection.order_item_id,
                    "ok": False,
                    "status_code": error.status_code,
                    "message": str(error.detail),
                }
            )
        except IntegrityError:
            db.rollback()
            results.append(
                {
                    "order_item_id": selection.order_item_id,
                    "ok": False,
                    "status_code": 409,
                    "message": "该明细状态已变化或已经在等候报料中，请刷新后重试",
                }
            )
    success_count = sum(1 for result in results if result["ok"])
    return {
        "items": results,
        "success_count": success_count,
        "failed_count": len(results) - success_count,
    }


@router.post("/holds/auto-release")
def auto_release_requisition_holds(
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    released_ids = _auto_release_requisition_holds(db, user=user)
    return {
        "released_hold_ids": released_ids,
        "released_count": len(released_ids),
    }


@router.get("/holds")
def list_requisition_holds(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    customer_id: int | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    condition_status: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    normalized_status = (condition_status or "").strip().lower()
    if normalized_status and normalized_status not in {"waiting", "due", "anomaly"}:
        raise HTTPException(
            status_code=422,
            detail="等候状态仅允许 waiting、due 或 anomaly",
        )
    allowed = _allowed_customer_ids(user, db)
    query = select(RequisitionHold).where(
        RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
    )
    if allowed is not None:
        query = query.where(RequisitionHold.customer_id_snapshot.in_(allowed))
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
        query = query.where(RequisitionHold.customer_id_snapshot == customer_id)
    if product_code and product_code.strip():
        query = query.where(
            RequisitionHold.product_code_snapshot.ilike(
                f"%{product_code.strip()}%"
            )
        )
    if product_name and product_name.strip():
        query = query.where(
            RequisitionHold.product_name_snapshot.ilike(
                f"%{product_name.strip()}%"
            )
        )
    if date_from is not None:
        query = query.where(RequisitionHold.expected_requisition_date >= date_from)
    if date_to is not None:
        query = query.where(RequisitionHold.expected_requisition_date <= date_to)
    holds = db.scalars(
        query.order_by(RequisitionHold.created_at.desc(), RequisitionHold.id.desc())
    ).all()
    items = [_requisition_hold_dict(db, hold) for hold in holds]
    if normalized_status:
        items = [
            item
            for item in items
            if item.get("condition_status") == normalized_status
        ]
    warning_count = sum(1 for item in items if item.get("warning"))
    due_count = sum(1 for item in items if item.get("release_ready"))
    total = len(items)
    page_items = items[(page - 1) * page_size : page * page_size]
    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "warning_count": warning_count,
        "due_count": due_count,
        "auto_released_hold_ids": [],
    }


@router.put("/holds/{hold_id}")
def update_requisition_hold(
    hold_id: int,
    payload: RequisitionHoldUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    hold = db.scalar(
        select(RequisitionHold)
        .where(RequisitionHold.id == hold_id)
        .with_for_update()
    )
    if hold is None:
        raise HTTPException(status_code=404, detail="等候报料记录不存在")
    _require_requisition_hold_customer_access(db, hold=hold, user=user)
    if hold.status != _REQUISITION_HOLD_ACTIVE or hold.order_item_id is None:
        raise HTTPException(status_code=409, detail="该记录已经恢复或失效，请刷新")
    if int(hold.version or 0) != payload.expected_version:
        raise HTTPException(status_code=409, detail="记录已被修改，请刷新后重试")
    before = _requisition_hold_audit_state(hold)
    try:
        item, order, _customer, _product = _ensure_requisition_hold_eligible(
            db,
            hold.order_item_id,
            user=user,
            allow_existing_hold=True,
            lock=True,
        )
    except HTTPException as error:
        if error.status_code == 403:
            raise
        note = f"修改条件时发现业务状态已变化：{error.detail}"
        invalidated = _invalidate_requisition_hold(
            db,
            hold=hold,
            user=user,
            source="manual_update_eligibility_changed",
            note=note,
            expected_version=payload.expected_version,
        )
        if invalidated:
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=409, detail=f"等候记录已关闭：{error.detail}")
    previous_item: OrderItem | None = None
    if payload.release_mode == "previous_batch_completed":
        assert payload.previous_order_item_id is not None
        previous_item = _validate_previous_batch_choice(
            db,
            item=item,
            order=order,
            previous_order_item_id=payload.previous_order_item_id,
        )
    else:
        assert payload.expected_requisition_date is not None
        if payload.expected_requisition_date <= beijing_today():
            raise HTTPException(
                status_code=409,
                detail="预计恢复报料日期必须晚于今天",
            )
    next_version = payload.expected_version + 1
    result = db.execute(
        update(RequisitionHold)
        .where(
            RequisitionHold.id == hold.id,
            RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
            RequisitionHold.version == payload.expected_version,
        )
        .values(
            release_mode=payload.release_mode,
            previous_order_item_id=previous_item.id if previous_item else None,
            previous_order_item_id_snapshot=(
                previous_item.id if previous_item else None
            ),
            expected_requisition_date=payload.expected_requisition_date,
            updated_by=user.id,
            version=next_version,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="记录已被修改，请刷新后重试")
    db.flush()
    db.refresh(hold)
    _append_requisition_hold_audit(
        db,
        hold=hold,
        user=user,
        action_code="requisition.hold.update",
        legacy_action="UPDATE_REQUISITION_HOLD",
        result="success",
        source="web",
        description="修改等候报料条件",
        transition_source="manual_update",
        before=before,
        after=_requisition_hold_audit_state(hold),
    )
    db.commit()
    db.refresh(hold)
    return _requisition_hold_dict(db, hold)


@router.post("/holds/{hold_id}/release")
def release_requisition_hold(
    hold_id: int,
    payload: RequisitionHoldReleasePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    hold = db.scalar(
        select(RequisitionHold)
        .where(RequisitionHold.id == hold_id)
        .with_for_update()
    )
    if hold is None:
        raise HTTPException(status_code=404, detail="等候报料记录不存在")
    _require_requisition_hold_customer_access(db, hold=hold, user=user)
    if hold.status != _REQUISITION_HOLD_ACTIVE or hold.order_item_id is None:
        raise HTTPException(status_code=409, detail="该记录已经恢复或失效，请刷新")
    if int(hold.version or 0) != payload.expected_version:
        raise HTTPException(status_code=409, detail="记录已被修改，请刷新后重试")
    try:
        item, _order, _customer, _product = _ensure_requisition_hold_eligible(
            db,
            hold.order_item_id,
            user=user,
            allow_existing_hold=True,
            lock=True,
        )
    except HTTPException as error:
        if error.status_code == 403:
            raise
        note = f"恢复时发现业务状态已变化：{error.detail}"
        invalidated = _invalidate_requisition_hold(
            db,
            hold=hold,
            user=user,
            source="manual_release_eligibility_changed",
            note=note,
            expected_version=payload.expected_version,
        )
        if invalidated:
            db.commit()
        else:
            db.rollback()
        raise HTTPException(status_code=409, detail=f"等候记录已关闭：{error.detail}")
    released = _release_requisition_hold(
        db,
        hold=hold,
        user=user,
        source="manual",
        note="人工恢复待报料",
        expected_version=payload.expected_version,
    )
    if not released:
        db.rollback()
        raise HTTPException(status_code=409, detail="记录已被修改，请刷新后重试")
    db.commit()
    db.refresh(hold)
    return _requisition_hold_dict(db, hold)


def _pending_requisition_candidates(
    db: Session,
    user: User,
    *,
    merge_group_ids: set[int] | None = None,
    order_item_ids: set[int] | None = None,
) -> tuple[list[Requisition], list[tuple[OrderItem, Order, Customer, Product]]]:
    allowed = _allowed_customer_ids(user, db)
    merge_group_query = (
        select(Requisition)
        .options(selectinload(Requisition.items))
        .where(Requisition.status == "merged_pending")
        .order_by(Requisition.created_at.desc(), Requisition.id.desc())
    )
    if merge_group_ids is not None:
        merge_group_query = merge_group_query.where(
            Requisition.id.in_(merge_group_ids)
        )
    merge_groups = db.scalars(
        _apply_requisition_scope(merge_group_query, user, db)
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
    held_order_item_ids = set(
        db.scalars(
            select(RequisitionHold.order_item_id).where(
                RequisitionHold.status == _REQUISITION_HOLD_ACTIVE,
                RequisitionHold.order_item_id.is_not(None),
            )
        ).all()
    )
    base_query = (
        select(OrderItem, Order, Customer, Product)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            OrderItem.requisition_status.in_(["未报料", "已报料"]),
            OrderItem.material_status == "pending",
            OrderItem.supply_mode_snapshot != "external_purchase",
            Order.status.notin_(["cancelled", "dead", "closed", "archived"]),
            OrderItem.is_force_closed.is_(False),
        )
    )
    if merged_order_item_ids:
        base_query = base_query.where(~OrderItem.id.in_(merged_order_item_ids))
    if held_order_item_ids:
        base_query = base_query.where(~OrderItem.id.in_(held_order_item_ids))
    if order_item_ids is not None:
        base_query = base_query.where(OrderItem.id.in_(order_item_ids))
    if allowed is not None:
        base_query = base_query.where(Order.customer_id.in_(allowed))
    rows = db.execute(
        base_query.order_by(OrderItem.created_at.desc(), OrderItem.id.desc())
    ).all()
    return merge_groups, rows


def _pending_merge_member_rows(
    db: Session,
    group_ids: list[int],
) -> dict[int, list[tuple[RequisitionItem, OrderItem, Order, Customer, Product]]]:
    if not group_ids:
        return {}
    grouped: dict[
        int, list[tuple[RequisitionItem, OrderItem, Order, Customer, Product]]
    ] = {}
    rows = db.execute(
        select(
            RequisitionItem.requisition_id,
            RequisitionItem,
            OrderItem,
            Order,
            Customer,
            Product,
        )
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(RequisitionItem.requisition_id.in_(group_ids))
        .order_by(RequisitionItem.requisition_id, RequisitionItem.id)
    ).all()
    for group_id, requisition_item, item, order, customer, product in rows:
        grouped.setdefault(int(group_id), []).append(
            (requisition_item, item, order, customer, product)
        )
    return grouped


def _dashboard_pending_requisition_item_is_eligible(
    db: Session,
    *,
    item: OrderItem,
    product: Product,
    context: _PendingRequisitionReadContext,
    finished_reserved_qty: int,
    active_requisition_qty: int,
) -> bool:
    if context.is_ordinary(item) and int(finished_reserved_qty or 0) <= 0:
        requirements = _ordinary_requisition_requirements(item)
        return (
            int(requirements.get("requisition_qty") or 0)
            - int(active_requisition_qty or 0)
            > 0
        )

    bom_snapshots = context.bom_snapshots_for(item)
    bom_components = context.bom_pending_component_requirements(
        db,
        item,
        snapshots=bom_snapshots,
    )
    if bom_components:
        parent_requirement = context.bom_pending_parent_requirement(
            item,
            finished_reserved_qty=finished_reserved_qty,
        )
        if _composite_parent_requisition_is_suppressed(item, bom_snapshots):
            parent_requirement["already_requisitioned"] = True
            parent_requirement["can_requisition"] = False
        return any(
            bool(requirement.get("can_requisition"))
            for requirement in [parent_requirement, *bom_components]
        )

    requirements = context.current_requisition_summary(
        item,
        product=product,
        finished_reserved_qty=finished_reserved_qty,
    )
    if not _requires_supplier_purchase(requirements):
        return False
    return (
        int(requirements.get("requisition_qty") or 0)
        - int(active_requisition_qty or 0)
        > 0
    )


def _pending_requisition_eligible_rows(db: Session, user: User) -> list[dict]:
    """Return authoritative eligible identities before full row decoration.

    P1-36J uses the public subset for the dashboard. P1-36K also keeps the
    canonical supplier label privately so filtering and pagination can happen
    before display materials, inventory previews, locations, and BOM payloads
    are constructed.
    """

    merge_groups, rows = _pending_requisition_candidates(db, user)
    merge_rows_by_group = _pending_merge_member_rows(
        db,
        [int(group.id) for group in merge_groups],
    )
    context_rows = list(rows)
    context_rows.extend(
        (item, order, customer, product)
        for group_rows in merge_rows_by_group.values()
        for _requisition_item, item, order, customer, product in group_rows
    )
    item_ids = [int(item.id) for item, *_ in context_rows]
    reservation_map = requisition_finished_inventory_coverage_by_item_ids(
        db,
        item_ids,
    )
    context = _PendingRequisitionReadContext(
        db,
        context_rows,
        include_display_facts=False,
    )
    active_requisition_map = _active_requisition_facts_by_item_ids(
        db,
        [int(item.id) for item, *_ in rows],
    )

    projected: list[dict] = []
    for group in merge_groups:
        group_rows = merge_rows_by_group.get(int(group.id), [])
        customer_names: list[str | None] = []
        product_codes: list[str | None] = []
        remaining_required_piece_qty = 0
        requisition_qty = 0
        for requisition_item, item, _order, customer, product in group_rows:
            component = _requisition_item_component(requisition_item)
            requirements = _current_requisition_requirements(
                None,
                item,
                cutting_mode=requisition_item.special_process,
                pieces_per_box=(
                    requisition_item.pieces_per_box or _pieces_per_box(item)
                ),
                finished_reserved_qty=reservation_map.get(int(item.id), 0),
                component_type=component,
                semi_reserved_piece_qty=context.semi_reserved_piece_qty(
                    int(item.id), component
                ),
            )
            if not _requires_supplier_purchase(requirements):
                continue
            customer_names.append(customer.name)
            product_codes.append(
                requisition_item.product_code_snapshot
                or item.snapshot_product_code
                or product.product_code
            )
            remaining_required_piece_qty += int(
                requirements.get("remaining_required_piece_qty") or 0
            )
            requisition_qty += int(requirements.get("requisition_qty") or 0)
        if remaining_required_piece_qty <= 0 or requisition_qty <= 0:
            continue
        projected.append(
            {
                "is_merge_group": True,
                "merge_group_id": int(group.id),
                "requisition_id": int(group.id),
                "item_id": f"mg{group.id}",
                "order_item_id": None,
                # The public merge row intentionally has no single customer ID;
                # keep it out of customer-specific todos while counting its
                # stable merge identity in the dashboard metric.
                "customer_id": None,
                "customer_name": " / ".join(_unique_text(customer_names)),
                "order_number": "合并组",
                "product_code": " / ".join(_unique_text(product_codes)),
                "delivery_date": None,
                "created_at": None,
                "_supplier_name": (
                    str(group.supplier_name or "").strip()
                    or "未设置供应商"
                ),
            }
        )

    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        active_requisition = active_requisition_map.get(
            int(item.id),
            {"quantity": 0},
        )
        if not _dashboard_pending_requisition_item_is_eligible(
            db,
            item=item,
            product=product,
            context=context,
            finished_reserved_qty=reservation_map.get(int(item.id), 0),
            active_requisition_qty=int(active_requisition.get("quantity") or 0),
        ):
            continue
        projected.append(
            {
                "is_merge_group": False,
                "merge_group_id": None,
                "requisition_id": None,
                "item_id": int(item.id),
                "order_item_id": int(item.id),
                "customer_id": int(customer.id),
                "customer_name": customer.name,
                "order_number": order.order_number,
                "product_code": item.snapshot_product_code or product.product_code,
                "delivery_date": order.delivery_date,
                # The public pending row does not expose created_at.  Keeping the
                # same null fallback preserves dashboard todo ordering exactly.
                "created_at": None,
                "_supplier_name": str(
                    item.snapshot_supplier_name or "未设置供应商"
                ).strip(),
            }
        )
    return projected


def dashboard_pending_requisition_rows(db: Session, user: User) -> list[dict]:
    """Return the P1-36J dashboard contract without pagination-only metadata."""

    return [
        {key: value for key, value in row.items() if key != "_supplier_name"}
        for row in _pending_requisition_eligible_rows(db, user)
    ]


def _pending_requisitions_full_payload(
    db: Session,
    user: User,
    *,
    merge_group_ids: set[int] | None = None,
    order_item_ids: set[int] | None = None,
) -> dict:
    merge_groups, rows = _pending_requisition_candidates(
        db,
        user,
        merge_group_ids=merge_group_ids,
        order_item_ids=order_item_ids,
    )
    if merge_group_ids is None and order_item_ids is None:
        registry = build_display_registry(db)
    else:
        merge_rows_by_group = _pending_merge_member_rows(
            db,
            [int(group.id) for group in merge_groups],
        )
        history_order_ids = {
            int(order.id)
            for _requisition_item, _item, order, _customer, _product in (
                group_row
                for group_rows in merge_rows_by_group.values()
                for group_row in group_rows
            )
            if is_history_order_number(order.order_number)
        }
        history_order_ids.update(
            int(order.id)
            for _item, order, _customer, _product in rows
            if is_history_order_number(order.order_number)
        )
        registry = build_display_registry_for_order_ids(db, history_order_ids)
    reservation_map = requisition_finished_inventory_coverage_by_item_ids(
        db, [item.id for item, *_ in rows]
    )
    read_context = _PendingRequisitionReadContext(db, rows)
    items = []
    for item, order, customer, product in rows:
        if is_history_order_number(order.order_number):
            continue
        material = read_context.material_for(item)
        if (
            read_context.is_ordinary(item)
            and int(reservation_map.get(item.id, 0)) <= 0
        ):
            requirements = _ordinary_requisition_requirements(item)
            cutting_mode = str(requirements["cutting_mode"])
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
                    "product_version": product.version,
                    "product_code": item.snapshot_product_code or product.product_code,
                    "product_name": item.snapshot_product_name,
                    "specification": item.snapshot_spec,
                    "material": item.snapshot_material,
                    "customer_material_code": item.snapshot_original_material_code
                    or item.snapshot_material,
                    "original_material_confidence": (
                        "frozen"
                        if item.snapshot_original_material_code
                        else "legacy_fallback"
                    ),
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
                    "finished_inventory_reserved_qty": 0,
                    "production_required_qty": int(
                        requirements["production_required_qty"]
                    ),
                    "fully_covered_by_finished_inventory": False,
                    "requisition_qty": int(requirements["requisition_qty"]),
                    "requisition_status": item.requisition_status,
                    "special_process": item.special_process,
                    "cutting_mode": cutting_mode,
                    "cutting_factor": int(requirements["cutting_factor"]),
                    "pieces_per_box": int(requirements["pieces_per_box"]),
                    "required_piece_qty": int(requirements["required_piece_qty"]),
                    "semi_finished_reserved_piece_qty": 0,
                    "remaining_required_piece_qty": int(
                        requirements["remaining_required_piece_qty"]
                    ),
                    "late_finished_inventory": {
                        "available_quantity": 0,
                        "reservable_quantity": 0,
                        "remaining_order_quantity": int(
                            requirements["production_required_qty"]
                        ),
                        "can_auto_reserve": False,
                        "blocked_reason": None,
                        "locations": [],
                        "lots": [],
                    },
                    "late_finished_inventory_available_qty": 0,
                    "late_finished_inventory_reservable_qty": 0,
                    "late_finished_inventory_locations": [],
                    "can_auto_use_late_finished_inventory": False,
                    "customer_board_preparation_available_piece_qty": 0,
                    "customer_board_preparation_available_sheet_qty": 0,
                    "can_auto_use_customer_board_preparation": False,
                    "component_requirements": requirements.get(
                        "component_requirements", []
                    ),
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
            continue
        bom_snapshots = read_context.bom_snapshots_for(item)
        bom_components = read_context.bom_pending_component_requirements(
            db,
            item,
            snapshots=bom_snapshots,
        )
        if bom_components:
            parent_requirement = read_context.bom_pending_parent_requirement(
                item,
                finished_reserved_qty=reservation_map.get(item.id, 0),
            )
            suppress_parent_requisition = _composite_parent_requisition_is_suppressed(
                item,
                bom_snapshots,
            )
            if suppress_parent_requisition:
                parent_requirement["already_requisitioned"] = True
                parent_requirement["can_requisition"] = False
            bom_sources = (
                bom_components
                if suppress_parent_requisition
                else [parent_requirement, *bom_components]
            )
            pending_sources = [row for row in bom_sources if row["can_requisition"]]
            if not pending_sources:
                continue
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
                    "is_composite_bom": True,
                    "suppress_parent_requisition": suppress_parent_requisition,
                    "order_number": display_order_number(order, registry),
                    "display_order_number": display_order_number(order, registry),
                    "customer_id": customer.id,
                    "customer_name": customer.name,
                    "product_id": product.id,
                    "product_version": product.version,
                    "product_code": item.snapshot_product_code or product.product_code,
                    "product_name": item.snapshot_product_name,
                    "specification": item.snapshot_spec,
                    "material": item.snapshot_material,
                    "customer_material_code": item.snapshot_original_material_code
                    or item.snapshot_material,
                    "original_material_confidence": (
                        "frozen"
                        if item.snapshot_original_material_code
                        else "legacy_fallback"
                    ),
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
                    "finished_inventory_reserved_qty": 0,
                    "production_required_qty": int(item.quantity or 0),
                    "fully_covered_by_finished_inventory": False,
                    "requisition_qty": sum(
                        int(row["requisition_qty"]) for row in pending_sources
                    ),
                    "requisition_status": item.requisition_status,
                    "special_process": item.special_process,
                    "cutting_mode": item.special_process or DEFAULT_CUTTING_MODE,
                    "cutting_factor": _cutting_factor(item.special_process),
                    "pieces_per_box": 1,
                    "required_piece_qty": sum(
                        int(row["required_piece_quantity"])
                        for row in pending_sources
                    ),
                    "semi_finished_reserved_piece_qty": sum(
                        int(row["semi_finished_reserved_piece_qty"])
                        for row in pending_sources
                    ),
                    "remaining_required_piece_qty": sum(
                        int(row["remaining_required_piece_qty"])
                        for row in pending_sources
                    ),
                    "component_requirements": bom_components,
                    "parent_requirement": parent_requirement,
                    "bom_requisition_sources": bom_sources,
                    "suggested_cardboard_len": item.cardboard_len or suggested_len,
                    "suggested_cardboard_width": item.cardboard_width or suggested_width,
                    "layer_count": item.layer_count,
                    "flute_type": item.flute_type,
                    "material_id": item.material_id,
                    "snapshot_supplier_name": item.snapshot_supplier_name,
                    "box_style": product.box_style,
                    "dimension_warnings": [],
                }
            )
            continue
        requirements = read_context.current_requisition_summary(
            item,
            product=product,
            finished_reserved_qty=reservation_map.get(item.id, 0),
        )
        pieces_per_box = int(requirements["pieces_per_box"])
        cutting_mode = str(requirements["cutting_mode"])
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        required_piece_qty = int(requirements["required_piece_qty"])
        semi_finished_reserved_piece_qty = int(
            requirements["semi_finished_reserved_piece_qty"]
        )
        remaining_required_piece_qty = int(
            requirements["remaining_required_piece_qty"]
        )
        if not _requires_supplier_purchase(requirements):
            continue
        suggested_len, suggested_width = _purchase_dimensions(
            item.snapshot_report_length_mm,
            item.snapshot_report_width_mm,
            DEFAULT_CUTTING_MODE,
        )
        if suggested_len is None or suggested_width is None:
            suggested_len, suggested_width = _suggested_dimensions(product)
        late_finished_inventory = read_context.late_finished_inventory_preview(
            item=item,
            requirements=requirements,
        )
        customer_board_preparation = read_context.customer_board_preparation_summary(
            item=item,
            product=product,
        )
        items.append(
            {
                "item_id": item.id,
                "is_merge_group": False,
                "order_number": display_order_number(order, registry),
                "display_order_number": display_order_number(order, registry),
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_id": product.id,
                "product_version": product.version,
                "product_code": item.snapshot_product_code or product.product_code,
                "product_name": item.snapshot_product_name,
                "specification": item.snapshot_spec,
                "material": item.snapshot_material,
                "customer_material_code": item.snapshot_original_material_code
                or item.snapshot_material,
                "original_material_confidence": (
                    "frozen"
                    if item.snapshot_original_material_code
                    else "legacy_fallback"
                ),
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
                "semi_finished_reserved_piece_qty": semi_finished_reserved_piece_qty,
                "remaining_required_piece_qty": remaining_required_piece_qty,
                "late_finished_inventory": late_finished_inventory,
                "late_finished_inventory_available_qty": int(
                    late_finished_inventory["available_quantity"]
                ),
                "late_finished_inventory_reservable_qty": int(
                    late_finished_inventory["reservable_quantity"]
                ),
                "late_finished_inventory_locations": late_finished_inventory[
                    "locations"
                ],
                "can_auto_use_late_finished_inventory": bool(
                    late_finished_inventory["can_auto_reserve"]
                ),
                "customer_board_preparation_available_piece_qty": int(
                    customer_board_preparation["available_piece_quantity"]
                ),
                "customer_board_preparation_available_sheet_qty": int(
                    customer_board_preparation["available_sheet_quantity"]
                ),
                "can_auto_use_customer_board_preparation": bool(
                    customer_board_preparation["has_option"]
                ),
                "component_requirements": requirements.get(
                    "component_requirements", []
                ),
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
    item_models = {item.id: item for item, *_ in rows}
    active_requisition_map = _active_requisition_facts_by_item_ids(
        db,
        list(item_models),
    )
    remaining_items: list[dict] = []
    for row in items:
        if row.get("is_composite_bom"):
            remaining_items.append(row)
            continue
        item_model = item_models.get(int(row.get("item_id") or 0))
        if item_model is None:
            continue
        active_requisition = active_requisition_map.get(
            item_model.id,
            {
                "source_key": f"order_item:{item_model.id}",
                "quantity": 0,
                "orders": [],
            },
        )
        theoretical_qty = int(row.get("requisition_qty") or 0)
        remaining_qty = max(
            theoretical_qty - int(active_requisition["quantity"]),
            0,
        )
        if remaining_qty <= 0:
            continue
        row["theoretical_requisition_qty"] = theoretical_qty
        row["already_requisitioned_qty"] = int(active_requisition["quantity"])
        row["remaining_requisition_qty"] = remaining_qty
        row["requisition_qty"] = remaining_qty
        row["existing_supplier_orders"] = active_requisition["orders"]
        remaining_items.append(row)
    items = remaining_items
    merge_group_items = [
        _merge_group_dict(group, db, display_registry=registry)
        for group in merge_groups
    ]
    items = [
        row
        for row in merge_group_items
        if int(row.get("remaining_required_piece_qty") or 0) > 0
        and int(row.get("requisition_qty") or 0) > 0
    ] + items
    supplier_counts: dict[str, int] = {}
    for row in items:
        supplier = (row.get("supplier_name") or row.get("snapshot_supplier_name") or "未设置供应商").strip()
        supplier_counts[supplier] = supplier_counts.get(supplier, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "auto_released_hold_ids": [],
        "supplier_counts": [
            {"supplier_name": supplier, "count": count}
            for supplier, count in sorted(
                supplier_counts.items(), key=lambda entry: (-entry[1], entry[0])
            )
        ],
    }


def _pending_supplier_name(row: dict) -> str:
    if "_supplier_name" in row:
        return str(row.get("_supplier_name") or "").strip()
    value = (
        row.get("supplier_name")
        or row.get("snapshot_supplier_name")
        or "未设置供应商"
    )
    return str(value).strip()


def _pending_supplier_counts(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        supplier = _pending_supplier_name(row)
        counts[supplier] = counts.get(supplier, 0) + 1
    return [
        {"supplier_name": supplier, "count": count}
        for supplier, count in sorted(
            counts.items(), key=lambda entry: (-entry[1], entry[0])
        )
    ]


@router.get("/pending")
def pending_requisitions(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=200)] = None,
    supplier_name: Annotated[str | None, Query(max_length=200)] = None,
) -> dict:
    user = _user
    if page is None and page_size is None and supplier_name is None:
        return _pending_requisitions_full_payload(db, user)

    eligible_rows = _pending_requisition_eligible_rows(db, user)
    overall_total = len(eligible_rows)
    supplier_counts = _pending_supplier_counts(eligible_rows)
    normalized_supplier_name = (
        str(supplier_name).strip() if supplier_name is not None else None
    )
    filtered_rows = (
        [
            row
            for row in eligible_rows
            if _pending_supplier_name(row) == normalized_supplier_name
        ]
        if normalized_supplier_name is not None
        else eligible_rows
    )
    total = len(filtered_rows)
    resolved_page_size = min(max(int(page_size or 25), 1), 200)
    requested_page = max(int(page or 1), 1)
    last_page = max(1, (total + resolved_page_size - 1) // resolved_page_size)
    resolved_page = min(requested_page, last_page)
    start = (resolved_page - 1) * resolved_page_size
    selected_rows = filtered_rows[start : start + resolved_page_size]

    selected_merge_group_ids = {
        int(row["merge_group_id"])
        for row in selected_rows
        if row.get("is_merge_group")
    }
    selected_order_item_ids = {
        int(row["order_item_id"])
        for row in selected_rows
        if not row.get("is_merge_group")
    }
    if selected_rows:
        page_payload = _pending_requisitions_full_payload(
            db,
            user,
            merge_group_ids=selected_merge_group_ids,
            order_item_ids=selected_order_item_ids,
        )
        decorated_by_identity = {
            (
                "merge",
                int(row.get("merge_group_id") or row.get("id")),
            )
            if row.get("is_merge_group")
            else ("item", int(row["item_id"])): row
            for row in page_payload["items"]
        }
        items = [
            decorated_by_identity[identity]
            for row in selected_rows
            if (
                identity := (
                    ("merge", int(row["merge_group_id"]))
                    if row.get("is_merge_group")
                    else ("item", int(row["order_item_id"]))
                )
            )
            in decorated_by_identity
        ]
    else:
        items = []

    return {
        "items": items,
        "total": total,
        "overall_total": overall_total,
        "page": resolved_page,
        "page_size": resolved_page_size,
        "auto_released_hold_ids": [],
        "supplier_counts": supplier_counts,
    }


@router.get("/pending/{item_id}/material-candidates")
def pending_material_candidates(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    item = _item_or_404(db, item_id)
    _, customer = _order_customer_for_item(db, item, user)
    original_is_frozen = bool(
        str(item.snapshot_original_material_code or "").strip()
    )
    original_code = (
        item.snapshot_original_material_code
        if original_is_frozen
        else item.snapshot_material
    )
    normalized_original = normalize_material_candidate_key(original_code)
    product = db.get(Product, item.product_id) if item.product_id else None
    if not normalized_original:
        return {
            "item_id": item.id,
            "customer_id": customer.id,
            "customer_name": customer.name,
            "original_material_code": None,
            "original_material_confidence": "unknown",
            "product_id": item.product_id,
            "product_version": product.version if product is not None else None,
            "candidates": [],
        }

    candidates = db.scalars(
        select(CustomerMaterialCandidate)
        .where(
            CustomerMaterialCandidate.customer_id == customer.id,
            CustomerMaterialCandidate.normalized_original_material_code
            == normalized_original,
            CustomerMaterialCandidate.is_active.is_(True),
        )
        .order_by(
            CustomerMaterialCandidate.manual_priority.desc(),
            CustomerMaterialCandidate.id.asc(),
        )
    ).all()
    history_rows = db.execute(
        select(
            CustomerMaterialSelectionHistory.selected_material_id,
            CustomerMaterialSelectionHistory.selected_supplier_name_snapshot,
            func.count(CustomerMaterialSelectionHistory.id),
            func.max(CustomerMaterialSelectionHistory.selected_at),
        )
        .where(
            CustomerMaterialSelectionHistory.customer_id == customer.id,
            CustomerMaterialSelectionHistory.normalized_original_material_code_snapshot
            == normalized_original,
        )
        .group_by(
            CustomerMaterialSelectionHistory.selected_material_id,
            CustomerMaterialSelectionHistory.selected_supplier_name_snapshot,
        )
    ).all()
    history_stats = {
        (
            material_id,
            normalize_supplier_candidate_key(supplier_name),
        ): (int(history_count or 0), last_used_at)
        for material_id, supplier_name, history_count, last_used_at in history_rows
    }
    eligible: list[tuple[CustomerMaterialCandidate, Material, int, datetime | None]] = []
    for candidate in candidates:
        if candidate.actual_material_id is None:
            continue
        material = db.get(Material, candidate.actual_material_id)
        if material is None or not material.is_active:
            continue
        if item.layer_count is not None and material.layer_count != item.layer_count:
            continue
        if (
            material.flute_type
            and item.flute_type
            and normalize_flute_type(material.flute_type)
            != normalize_flute_type(item.flute_type)
        ):
            continue
        if validate_flute_for_write(item.flute_type, material.layer_count):
            continue
        supplier_name = material.supplier_name or candidate.supplier_name
        history_count, last_used_at = history_stats.get(
            (material.id, normalize_supplier_candidate_key(supplier_name)),
            (0, None),
        )
        eligible.append((candidate, material, history_count, last_used_at))
    eligible.sort(
        key=lambda entry: (
            entry[0].manual_priority,
            entry[2],
            entry[3] or datetime.min,
            entry[1].quote_price is not None
            or entry[1].rule_base_price is not None,
            -entry[0].id,
        ),
        reverse=True,
    )
    rows = []
    for index, (candidate, material, history_count, last_used_at) in enumerate(
        eligible
    ):
        row = candidate_response(
            candidate,
            material,
            history_count=history_count,
            last_used_at=last_used_at,
            recommended=index == 0,
        )
        row["id"] = candidate.id
        row["flute_type"] = item.flute_type
        if not has_permission(user, "cost.view"):
            row.pop("reference_price", None)
            row.pop("price_unit", None)
        rows.append(row)
    return {
        "item_id": item.id,
        "customer_id": customer.id,
        "customer_name": customer.name,
        "original_material_code": original_code,
        "original_material_confidence": "frozen" if original_is_frozen else "legacy_fallback",
        "current_material_id": item.material_id,
        "current_material_code": item.snapshot_material,
        "product_id": item.product_id,
        "product_version": product.version if product is not None else None,
        "candidates": rows,
    }


def _material_context_original_code(
    product: Product,
    current_material: Material | None,
) -> str:
    return str(
        product.legacy_material_text
        or product.default_material_code
        or (current_material.code if current_material is not None else "")
        or ""
    ).strip()


def _supplier_item_snapshot_values(
    db: Session,
    order_item: OrderItem | None,
    *,
    fallback_material_id: int | None,
    fallback_supplier_name: str | None,
    fallback_layer_count: int | None,
    fallback_flute_type: str | None,
) -> dict:
    material_id = (
        order_item.material_id
        if order_item is not None and order_item.material_id is not None
        else fallback_material_id
    )
    material = db.get(Material, material_id) if material_id is not None else None
    material_code = (
        material.code
        if material is not None
        else _clean_supplier_material_code(
            order_item.snapshot_material if order_item is not None else None,
            (
                order_item.layer_count
                if order_item is not None and order_item.layer_count is not None
                else fallback_layer_count
            ),
        )
        or None
    )
    supplier_name = (
        (material.supplier_name if material is not None else None)
        or (
            order_item.snapshot_supplier_name
            if order_item is not None
            else None
        )
        or fallback_supplier_name
    )
    return {
        "product_id": order_item.product_id if order_item is not None else None,
        "material_id": material_id,
        "material_code_snapshot": material_code,
        "supplier_name_snapshot": supplier_name,
        "layer_count_snapshot": (
            order_item.layer_count
            if order_item is not None and order_item.layer_count is not None
            else (
                material.layer_count
                if material is not None
                else fallback_layer_count
            )
        ),
        "flute_type_snapshot": (
            order_item.flute_type
            if order_item is not None and order_item.flute_type
            else fallback_flute_type
        ),
    }


def _is_confirmed_supplier_order(order: SupplierRequisitionOrder) -> bool:
    return str(order.status or "").strip().lower() == "confirmed"


def _basis_weight_total_gsm(description: str | None) -> float | None:
    text_value = str(description or "").strip()
    if not text_value:
        return None
    values = re.findall(r"(\d+(?:\.\d+)?)\s*(?:g|克)", text_value, flags=re.I)
    if not values and re.fullmatch(r"[\d.\s/|｜,，]+", text_value):
        values = re.findall(r"\d+(?:\.\d+)?", text_value)
    if not values:
        return None
    return float(sum(Decimal(value) for value in values))


def _current_material_comparison(
    db: Session,
    *,
    material: Material | None,
    flute_type: str | None,
    include_cost: bool,
) -> dict:
    """Return current master-data facts without pretending they are requisition snapshots."""
    if material is None:
        return {}
    result = {
        "basis_weight_description": material.basis_weight_description,
        "total_basis_weight_gsm": _basis_weight_total_gsm(
            material.basis_weight_description
        ),
        "paper_composition": material.paper_composition,
        "material_is_active": material.is_active,
    }
    if not include_cost:
        return result
    base_price = (
        material.quote_price
        if material.quote_price is not None
        else material.rule_base_price
    )
    effective = material_pricing.get_effective_material_price(
        db,
        material=material,
        base_price=base_price,
        flute_type=flute_type,
    )
    result.update(
        {
            "reference_price": effective["base_price"],
            "flute_delta": effective["flute_delta"],
            "effective_price": effective["effective_price"],
            "price_unit": material.price_unit,
            "quote_date": material.quote_date,
            "price_source": material.price_source,
            "price_scope": "current_reference",
        }
    )
    return result


@router.get("/products/{product_id}/material-context")
def product_material_context(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="常用箱不存在")
    require_customer_access(product.customer_id, user, db)
    can_view_costs = has_permission(user, "cost.view")

    current_material = (
        db.get(Material, product.material_id)
        if product.material_id is not None
        else None
    )
    original_code = _material_context_original_code(product, current_material)
    normalized_original = normalize_material_candidate_key(original_code)

    official_rows = db.execute(
        select(
            SupplierRequisitionOrderItem,
            SupplierRequisitionOrder,
            OrderItem,
        )
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .outerjoin(OrderItem, OrderItem.id == SupplierRequisitionOrderItem.order_item_id)
        .where(
            or_(
                SupplierRequisitionOrderItem.product_id == product.id,
                and_(
                    SupplierRequisitionOrderItem.product_id.is_(None),
                    OrderItem.product_id == product.id,
                ),
            )
        )
        .order_by(
            SupplierRequisitionOrder.created_at.desc(),
            SupplierRequisitionOrderItem.id.desc(),
        )
    ).all()

    official_history: list[dict] = []
    official_order_item_ids: set[int] = set()
    official_usage: dict[tuple[int | None, str, str], dict] = {}
    for supplier_item, supplier_order, order_item in official_rows:
        if supplier_item.order_item_id is not None:
            official_order_item_ids.add(supplier_item.order_item_id)
        material_id = supplier_item.material_id
        if material_id is None and order_item is not None:
            material_id = order_item.material_id
        material = db.get(Material, material_id) if material_id is not None else None
        material_code = (
            supplier_item.material_code_snapshot
            or (material.code if material is not None else None)
            or (order_item.snapshot_material if order_item is not None else None)
        )
        supplier_name = (
            supplier_item.supplier_name_snapshot
            or supplier_order.supplier_name
            or (
                order_item.snapshot_supplier_name
                if order_item is not None
                else None
            )
        )
        confirmed = _is_confirmed_supplier_order(supplier_order)
        row = {
            "source_type": "supplier_order",
            "source_confidence": (
                "official_snapshot"
                if supplier_item.material_code_snapshot
                else "derived_from_order_item"
            ),
            "document_id": supplier_order.id,
            "document_item_id": supplier_item.id,
            "document_no": supplier_order.order_number,
            "document_date": supplier_order.created_at,
            "document_status": supplier_order.status,
            "is_effective": confirmed,
            "order_item_id": supplier_item.order_item_id,
            "item_order_number": supplier_item.order_number,
            "product_id": product.id,
            "material_id": material_id,
            "material_code": material_code,
            "supplier_name": supplier_name,
            "layer_count": (
                supplier_item.layer_count_snapshot
                if supplier_item.layer_count_snapshot is not None
                else supplier_order.layer_count
            ),
            "flute_type": (
                supplier_item.flute_type_snapshot
                or supplier_order.flute_type
            ),
            "requisition_qty": supplier_item.requisition_qty,
        }
        comparison = _current_material_comparison(
            db,
            material=material,
            flute_type=row["flute_type"],
            include_cost=can_view_costs,
        )
        snapshot_weight = (
            order_item.snapshot_weight
            if order_item is not None
            and order_item.snapshot_weight
            and order_item.material_id == material_id
            else None
        )
        row.update(
            {
                "basis_weight_description": (
                    snapshot_weight or comparison.get("basis_weight_description")
                ),
                "weight_source": (
                    "order_snapshot"
                    if snapshot_weight
                    else (
                        "current_material_master"
                        if comparison.get("basis_weight_description")
                        else None
                    )
                ),
                "paper_composition": comparison.get("paper_composition"),
                "total_basis_weight_gsm": (
                    _basis_weight_total_gsm(snapshot_weight)
                    if snapshot_weight
                    else comparison.get("total_basis_weight_gsm")
                ),
                "material_is_active": comparison.get("material_is_active"),
            }
        )
        if can_view_costs:
            row.update(
                {
                    "current_reference_price": comparison.get("reference_price"),
                    "current_flute_delta": comparison.get("flute_delta"),
                    "current_effective_price": comparison.get("effective_price"),
                    "current_price_unit": comparison.get("price_unit"),
                    "current_quote_date": comparison.get("quote_date"),
                    "current_price_source": comparison.get("price_source"),
                    "current_price_scope": "current_reference",
                }
            )
        official_history.append(row)
        if confirmed:
            key = (
                material_id,
                normalize_material_candidate_key(material_code),
                normalize_supplier_candidate_key(supplier_name),
            )
            stat = official_usage.setdefault(
                key,
                {
                    "count": 0,
                    "last_used_at": None,
                    "last_document_no": None,
                    "material_id": material_id,
                    "material_code": material_code,
                    "supplier_name": supplier_name,
                    "layer_count": row["layer_count"],
                    "flute_type": row["flute_type"],
                },
            )
            stat["count"] += 1
            if (
                stat["last_used_at"] is None
                or (supplier_order.created_at or datetime.min) > stat["last_used_at"]
            ):
                stat["last_used_at"] = supplier_order.created_at
                stat["last_document_no"] = supplier_order.order_number
                stat["material_code"] = material_code
                stat["supplier_name"] = supplier_name
                stat["layer_count"] = row["layer_count"]
                stat["flute_type"] = row["flute_type"]

    legacy_rows = db.execute(
        select(RequisitionItem, Requisition, OrderItem)
        .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(
            OrderItem.product_id == product.id,
            func.lower(RequisitionItem.status).notin_(
                {
                    "merged_pending",
                    "supplier_requisition_created",
                    "cancelled",
                    "voided",
                    "withdrawn",
                    "invalid",
                }
            ),
            func.lower(Requisition.status).notin_(
                {
                    "merged_pending",
                    "supplier_requisition_created",
                    "cancelled",
                    "voided",
                    "withdrawn",
                    "invalid",
                }
            ),
        )
        .order_by(Requisition.requisition_date.desc(), RequisitionItem.id.desc())
    ).all()
    legacy_history: list[dict] = []
    for requisition_item, requisition, order_item in legacy_rows:
        if requisition_item.order_item_id in official_order_item_ids:
            continue
        row = {
            "source_type": "legacy_material_requisition",
            "source_confidence": "legacy_snapshot",
            "document_id": requisition.id,
            "document_item_id": requisition_item.id,
            "document_no": requisition.requisition_number,
            "document_date": requisition.requisition_date,
            "document_status": requisition.status,
            "is_effective": True,
            "order_item_id": requisition_item.order_item_id,
            "item_order_number": order_item.item_order_number,
            "product_id": product.id,
            "material_id": order_item.material_id,
            "material_code": requisition_item.material_snapshot,
            "supplier_name": requisition.supplier_name,
            "layer_count": order_item.layer_count,
            "flute_type": order_item.flute_type,
            "requisition_qty": requisition_item.requisition_qty,
        }
        legacy_history.append(row)
        material = (
            db.get(Material, order_item.material_id)
            if order_item.material_id is not None
            else None
        )
        comparison = _current_material_comparison(
            db,
            material=material,
            flute_type=row["flute_type"],
            include_cost=can_view_costs,
        )
        snapshot_weight = order_item.snapshot_weight or None
        row.update(
            {
                "basis_weight_description": (
                    snapshot_weight or comparison.get("basis_weight_description")
                ),
                "weight_source": (
                    "order_snapshot"
                    if snapshot_weight
                    else (
                        "current_material_master"
                        if comparison.get("basis_weight_description")
                        else None
                    )
                ),
                "paper_composition": comparison.get("paper_composition"),
                "total_basis_weight_gsm": (
                    _basis_weight_total_gsm(snapshot_weight)
                    if snapshot_weight
                    else comparison.get("total_basis_weight_gsm")
                ),
                "material_is_active": comparison.get("material_is_active"),
            }
        )
        if can_view_costs:
            row.update(
                {
                    "current_reference_price": comparison.get("reference_price"),
                    "current_flute_delta": comparison.get("flute_delta"),
                    "current_effective_price": comparison.get("effective_price"),
                    "current_price_unit": comparison.get("price_unit"),
                    "current_quote_date": comparison.get("quote_date"),
                    "current_price_source": comparison.get("price_source"),
                    "current_price_scope": "current_reference",
                }
            )
        material_code = requisition_item.material_snapshot
        supplier_name = requisition.supplier_name
        key = (
            order_item.material_id,
            normalize_material_candidate_key(material_code),
            normalize_supplier_candidate_key(supplier_name),
        )
        used_at = datetime.combine(requisition.requisition_date, datetime.min.time())
        stat = official_usage.setdefault(
            key,
            {
                "count": 0,
                "last_used_at": None,
                "last_document_no": None,
                "material_id": order_item.material_id,
                "material_code": material_code,
                "supplier_name": supplier_name,
                "layer_count": order_item.layer_count,
                "flute_type": order_item.flute_type,
            },
        )
        stat["count"] += 1
        if stat["last_used_at"] is None or used_at > stat["last_used_at"]:
            stat["last_used_at"] = used_at
            stat["last_document_no"] = requisition.requisition_number
            stat["material_code"] = material_code
            stat["supplier_name"] = supplier_name

    history_rows = db.execute(
        select(CustomerMaterialSelectionHistory, OrderItem)
        .outerjoin(
            OrderItem,
            OrderItem.id == CustomerMaterialSelectionHistory.order_item_id,
        )
        .where(
            or_(
                CustomerMaterialSelectionHistory.product_id == product.id,
                and_(
                    CustomerMaterialSelectionHistory.product_id.is_(None),
                    OrderItem.product_id == product.id,
                ),
            )
        )
        .order_by(
            CustomerMaterialSelectionHistory.selected_at.desc(),
            CustomerMaterialSelectionHistory.id.desc(),
        )
    ).all()
    actor_ids = {
        history.selected_by
        for history, _order_item in history_rows
        if history.selected_by is not None
    }
    actors = (
        {
            actor.id: actor
            for actor in db.scalars(select(User).where(User.id.in_(actor_ids))).all()
        }
        if actor_ids
        else {}
    )
    manual_history = [
        {
            "id": history.id,
            "order_item_id": history.order_item_id,
            "candidate_id": history.candidate_id,
            "original_material_code": history.original_material_code_snapshot,
            "material_id": history.selected_material_id,
            "material_code": history.selected_material_code_snapshot,
            "supplier_name": history.selected_supplier_name_snapshot,
            "layer_count": history.layer_count_snapshot,
            "flute_type": history.flute_type_snapshot,
            "source_type": history.source_type,
            "source_reference": history.source_reference,
            "selection_reason": history.selection_reason,
            "selected_at": history.selected_at,
            "selected_by": history.selected_by,
            "selected_by_name": (
                actors[history.selected_by].display_name
                or actors[history.selected_by].real_name
                or actors[history.selected_by].username
            )
            if history.selected_by in actors
            else None,
        }
        for history, _order_item in history_rows
    ]
    manual_usage: dict[tuple[int | None, str, str], dict] = {}
    for row in manual_history:
        material_id = row.get("material_id")
        material = db.get(Material, material_id) if material_id is not None else None
        comparison = _current_material_comparison(
            db,
            material=material,
            flute_type=row.get("flute_type") or product.flute_type,
            include_cost=can_view_costs,
        )
        row.update(
            {
                "basis_weight_description": comparison.get(
                    "basis_weight_description"
                ),
                "weight_source": (
                    "current_material_master"
                    if comparison.get("basis_weight_description")
                    else None
                ),
                "paper_composition": comparison.get("paper_composition"),
                "total_basis_weight_gsm": comparison.get(
                    "total_basis_weight_gsm"
                ),
                "material_is_active": comparison.get("material_is_active"),
            }
        )
        if can_view_costs:
            row.update(
                {
                    "current_reference_price": comparison.get("reference_price"),
                    "current_flute_delta": comparison.get("flute_delta"),
                    "current_effective_price": comparison.get("effective_price"),
                    "current_price_unit": comparison.get("price_unit"),
                    "current_quote_date": comparison.get("quote_date"),
                    "current_price_source": comparison.get("price_source"),
                    "current_price_scope": "current_reference",
                }
            )
        key = (
            material_id,
            normalize_material_candidate_key(row.get("material_code")),
            normalize_supplier_candidate_key(row.get("supplier_name")),
        )
        stat = manual_usage.setdefault(
            key,
            {
                "count": 0,
                "last_used_at": None,
                "material_id": material_id,
                "material_code": row.get("material_code"),
                "supplier_name": row.get("supplier_name"),
                "layer_count": row.get("layer_count"),
                "flute_type": row.get("flute_type"),
            },
        )
        stat["count"] += 1
        if (
            stat["last_used_at"] is None
            or row["selected_at"] > stat["last_used_at"]
        ):
            stat["last_used_at"] = row["selected_at"]

    candidates = []
    candidates_by_material_id: dict[int, dict] = {}
    if normalized_original:
        configured_candidates = db.scalars(
            select(CustomerMaterialCandidate)
            .where(
                CustomerMaterialCandidate.customer_id == product.customer_id,
                CustomerMaterialCandidate.normalized_original_material_code
                == normalized_original,
                CustomerMaterialCandidate.is_active.is_(True),
            )
            .order_by(
                CustomerMaterialCandidate.manual_priority.desc(),
                CustomerMaterialCandidate.id.asc(),
            )
        ).all()
        for candidate in configured_candidates:
            if candidate.actual_material_id is None:
                continue
            material = db.get(Material, candidate.actual_material_id)
            if material is None or not material.is_active:
                continue
            supplier_name = material.supplier_name or candidate.supplier_name
            stat = official_usage.get(
                (
                    material.id,
                    normalize_material_candidate_key(material.code),
                    normalize_supplier_candidate_key(supplier_name),
                ),
                {
                    "count": 0,
                    "last_used_at": None,
                    "last_document_no": None,
                },
            )
            reasons: list[str] = []
            if candidate.manual_priority > 0:
                reasons.append(f"人工优先级 {candidate.manual_priority}")
            if stat["count"] > 0:
                reasons.append(f"有效正式报料 {stat['count']} 次")
            if stat["last_document_no"]:
                reasons.append(f"最近单号 {stat['last_document_no']}")
            if not reasons:
                reasons.append("客户原始材质代码匹配")
            comparison = _current_material_comparison(
                db,
                material=material,
                flute_type=product.flute_type,
                include_cost=can_view_costs,
            )
            row = {
                "candidate_id": candidate.id,
                "candidate_key": f"configured-{candidate.id}",
                "material_id": material.id,
                "material_code": material.code,
                "supplier_name": supplier_name,
                "layer_count": material.layer_count,
                "flute_type": product.flute_type,
                "manual_priority": candidate.manual_priority,
                "effective_use_count": stat["count"],
                "history_count": stat["count"],
                "selection_history_count": 0,
                "last_requisition_at": stat["last_used_at"],
                "last_used_at": stat["last_used_at"],
                "last_document_no": stat["last_document_no"],
                "recommendation_reasons": reasons,
                "recommendation_reason": "；".join(reasons),
                "source": candidate.source,
                "notes": candidate.notes,
                "candidate_source_types": ["configured"],
                "selectable": True,
                **comparison,
            }
            if stat["count"] > 0:
                row["candidate_source_types"].append("formal_requisition_history")
            candidates.append(row)
            candidates_by_material_id[material.id] = row

    for stat in official_usage.values():
        material_id = stat.get("material_id")
        existing = candidates_by_material_id.get(material_id) if material_id else None
        if existing is not None:
            if "formal_requisition_history" not in existing["candidate_source_types"]:
                existing["candidate_source_types"].append("formal_requisition_history")
            continue
        material = db.get(Material, material_id) if material_id is not None else None
        material_code = (
            material.code if material is not None else stat.get("material_code")
        )
        supplier_name = (
            material.supplier_name
            if material is not None and material.supplier_name
            else stat.get("supplier_name")
        )
        comparison = _current_material_comparison(
            db,
            material=material,
            flute_type=product.flute_type or stat.get("flute_type"),
            include_cost=can_view_costs,
        )
        reasons = [f"有效正式报料 {stat['count']} 次"]
        if stat.get("last_document_no"):
            reasons.append(f"最近单号 {stat['last_document_no']}")
        row = {
            "candidate_id": None,
            "candidate_key": (
                f"history-material-{material_id}"
                if material_id is not None
                else "history-snapshot-"
                f"{normalize_material_candidate_key(material_code)}-"
                f"{normalize_supplier_candidate_key(supplier_name)}"
            ),
            "material_id": material_id,
            "material_code": material_code,
            "supplier_name": supplier_name,
            "layer_count": (
                material.layer_count if material is not None else stat.get("layer_count")
            ),
            "flute_type": product.flute_type or stat.get("flute_type"),
            "manual_priority": 0,
            "effective_use_count": stat["count"],
            "history_count": stat["count"],
            "selection_history_count": 0,
            "last_requisition_at": stat["last_used_at"],
            "last_used_at": stat["last_used_at"],
            "last_document_no": stat["last_document_no"],
            "recommendation_reasons": reasons,
            "recommendation_reason": "；".join(reasons),
            "source": "formal_requisition_history",
            "notes": None,
            "candidate_source_types": ["formal_requisition_history"],
            "selectable": bool(material is not None and material.is_active),
            **comparison,
        }
        candidates.append(row)
        if material_id is not None:
            candidates_by_material_id[material_id] = row

    if current_material is not None:
        existing = candidates_by_material_id.get(current_material.id)
        if existing is not None:
            if "current_product_material" not in existing["candidate_source_types"]:
                existing["candidate_source_types"].append("current_product_material")
        else:
            comparison = _current_material_comparison(
                db,
                material=current_material,
                flute_type=product.flute_type,
                include_cost=can_view_costs,
            )
            row = {
                "candidate_id": None,
                "candidate_key": f"current-material-{current_material.id}",
                "material_id": current_material.id,
                "material_code": current_material.code,
                "supplier_name": current_material.supplier_name,
                "layer_count": current_material.layer_count,
                "flute_type": product.flute_type,
                "manual_priority": 0,
                "effective_use_count": 0,
                "history_count": 0,
                "selection_history_count": 0,
                "last_requisition_at": None,
                "last_used_at": None,
                "last_document_no": None,
                "recommendation_reasons": ["常用箱当前材质"],
                "recommendation_reason": "常用箱当前材质",
                "source": "current_product_material",
                "notes": None,
                "candidate_source_types": ["current_product_material"],
                "selectable": current_material.is_active,
                **comparison,
            }
            candidates.append(row)
            candidates_by_material_id[current_material.id] = row

    for stat in manual_usage.values():
        material_id = stat.get("material_id")
        existing = candidates_by_material_id.get(material_id) if material_id else None
        if existing is not None:
            if "requisition_material_selection" not in existing["candidate_source_types"]:
                existing["candidate_source_types"].append(
                    "requisition_material_selection"
                )
            existing["selection_history_count"] = stat["count"]
            if (
                existing.get("last_used_at") is None
                or stat["last_used_at"] > existing["last_used_at"]
            ):
                existing["last_used_at"] = stat["last_used_at"]
            reason = f"报料选材记录 {stat['count']} 次"
            if reason not in existing["recommendation_reasons"]:
                existing["recommendation_reasons"].append(reason)
                existing["recommendation_reason"] = "；".join(
                    existing["recommendation_reasons"]
                )
            continue
        material = db.get(Material, material_id) if material_id is not None else None
        material_code = (
            material.code if material is not None else stat.get("material_code")
        )
        supplier_name = (
            material.supplier_name
            if material is not None and material.supplier_name
            else stat.get("supplier_name")
        )
        comparison = _current_material_comparison(
            db,
            material=material,
            flute_type=product.flute_type or stat.get("flute_type"),
            include_cost=can_view_costs,
        )
        reasons = [f"报料选材记录 {stat['count']} 次"]
        row = {
            "candidate_id": None,
            "candidate_key": (
                f"selection-material-{material_id}"
                if material_id is not None
                else "selection-snapshot-"
                f"{normalize_material_candidate_key(material_code)}-"
                f"{normalize_supplier_candidate_key(supplier_name)}"
            ),
            "material_id": material_id,
            "material_code": material_code,
            "supplier_name": supplier_name,
            "layer_count": (
                material.layer_count if material is not None else stat.get("layer_count")
            ),
            "flute_type": product.flute_type or stat.get("flute_type"),
            "manual_priority": 0,
            "effective_use_count": 0,
            "history_count": 0,
            "selection_history_count": stat["count"],
            "last_requisition_at": None,
            "last_used_at": stat["last_used_at"],
            "last_document_no": None,
            "recommendation_reasons": reasons,
            "recommendation_reason": "；".join(reasons),
            "source": "requisition_material_selection",
            "notes": None,
            "candidate_source_types": ["requisition_material_selection"],
            "selectable": bool(material is not None and material.is_active),
            **comparison,
        }
        candidates.append(row)
        if material_id is not None:
            candidates_by_material_id[material_id] = row

    candidates.sort(
        key=lambda row: (
            row["manual_priority"],
            row["effective_use_count"],
            row["last_used_at"] or datetime.min,
            "current_product_material" in row["candidate_source_types"],
            row.get("effective_price") is not None,
            -(row.get("material_id") or 0),
        ),
        reverse=True,
    )
    for index, row in enumerate(candidates):
        row["recommended"] = index == 0 and row["selectable"]
    if can_view_costs:
        effective_prices = [
            row["effective_price"]
            for row in candidates
            if row.get("effective_price") is not None
        ]
        lowest_price = min(effective_prices) if effective_prices else None
        for row in candidates:
            effective_price = row.get("effective_price")
            row["price_difference_to_lowest"] = (
                None
                if effective_price is None or lowest_price is None
                else round(effective_price - lowest_price, 4)
            )

    requisition_history = sorted(
        [*official_history, *legacy_history],
        key=lambda row: (
            (
                row["document_date"].isoformat()
                if row["document_date"] is not None
                else ""
            ),
            row["document_id"],
        ),
        reverse=True,
    )
    formal_event_signatures = {
        (
            row.get("order_item_id"),
            (
                f"material:{row['material_id']}"
                if row.get("material_id") is not None
                else "snapshot:"
                f"{normalize_material_candidate_key(row.get('material_code'))}:"
                f"{normalize_supplier_candidate_key(row.get('supplier_name'))}"
            ),
        )
        for row in requisition_history
    }
    selection_events = []
    for row in manual_history:
        signature = (
            row.get("order_item_id"),
            (
                f"material:{row['material_id']}"
                if row.get("material_id") is not None
                else "snapshot:"
                f"{normalize_material_candidate_key(row.get('material_code'))}:"
                f"{normalize_supplier_candidate_key(row.get('supplier_name'))}"
            ),
        )
        if signature in formal_event_signatures:
            continue
        event = {
            "source_type": "material_selection",
            "source_confidence": "selection_snapshot",
            "document_id": row["id"],
            "document_item_id": row["id"],
            "document_no": row.get("source_reference") or f"选材记录 #{row['id']}",
            "document_date": row["selected_at"],
            "document_status": "报料选材记录（未匹配正式报料单）",
            "is_effective": True,
            "is_formal_requisition": False,
            "order_item_id": row.get("order_item_id"),
            "item_order_number": None,
            "product_id": product.id,
            "material_id": row.get("material_id"),
            "material_code": row.get("material_code"),
            "supplier_name": row.get("supplier_name"),
            "layer_count": row.get("layer_count"),
            "flute_type": row.get("flute_type"),
            "requisition_qty": None,
            "basis_weight_description": row.get("basis_weight_description"),
            "weight_source": row.get("weight_source"),
            "paper_composition": row.get("paper_composition"),
            "total_basis_weight_gsm": row.get("total_basis_weight_gsm"),
            "material_is_active": row.get("material_is_active"),
            "selection_reason": row.get("selection_reason"),
            "selected_by_name": row.get("selected_by_name"),
        }
        if can_view_costs:
            event.update(
                {
                    "current_reference_price": row.get("current_reference_price"),
                    "current_flute_delta": row.get("current_flute_delta"),
                    "current_effective_price": row.get("current_effective_price"),
                    "current_price_unit": row.get("current_price_unit"),
                    "current_quote_date": row.get("current_quote_date"),
                    "current_price_source": row.get("current_price_source"),
                    "current_price_scope": "current_reference",
                }
            )
        selection_events.append(event)
    material_history = sorted(
        [*requisition_history, *selection_events],
        key=lambda row: (
            (
                row["document_date"].isoformat()
                if row["document_date"] is not None
                else ""
            ),
            row["document_id"],
        ),
        reverse=True,
    )
    if can_view_costs:
        effective_history_prices = [
            row["current_effective_price"]
            for row in material_history
            if row.get("is_effective")
            and row.get("current_effective_price") is not None
        ]
        lowest_history_price = (
            min(effective_history_prices) if effective_history_prices else None
        )
        for row in material_history:
            current_price = row.get("current_effective_price")
            row["current_price_difference_to_lowest"] = (
                None
                if not row.get("is_effective")
                or current_price is None
                or lowest_history_price is None
                else round(current_price - lowest_history_price, 4)
            )
    for row in requisition_history:
        document_date = row.get("document_date")
        if isinstance(document_date, datetime):
            row["document_date"] = utc_naive_to_api(document_date)
        elif isinstance(document_date, date):
            row["document_date"] = (
                f"{document_date.isoformat()}T00:00:00+08:00"
            )
    for row in selection_events:
        document_date = row.get("document_date")
        if isinstance(document_date, datetime):
            row["document_date"] = utc_naive_to_api(document_date)
    for row in candidates:
        if isinstance(row.get("last_used_at"), datetime):
            row["last_used_at"] = utc_naive_to_api(row["last_used_at"])
        if isinstance(row.get("last_requisition_at"), datetime):
            row["last_requisition_at"] = utc_naive_to_api(
                row["last_requisition_at"]
            )
    for row in manual_history:
        if isinstance(row.get("selected_at"), datetime):
            row["selected_at"] = utc_naive_to_api(row["selected_at"])
    return {
        "product_id": product.id,
        "product_version": product.version,
        "customer_id": product.customer_id,
        "original_material_code": original_code or None,
        "current_material": {
            "material_id": current_material.id if current_material is not None else None,
            "material_code": (
                current_material.code if current_material is not None else None
            ),
            "supplier_name": (
                current_material.supplier_name if current_material is not None else None
            ),
            "layer_count": product.layer_count,
            "flute_type": product.flute_type,
        },
        "candidates": candidates,
        "candidate_summary": candidates,
        "requisition_history": requisition_history,
        "material_history": material_history,
        "manual_selection_history": manual_history,
    }


@router.get("/pending/{item_id}/material-history")
def pending_material_history(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    item = _item_or_404(db, item_id)
    _order_customer_for_item(db, item, user)
    rows = db.scalars(
        select(CustomerMaterialSelectionHistory)
        .where(CustomerMaterialSelectionHistory.order_item_id == item.id)
        .order_by(
            CustomerMaterialSelectionHistory.selected_at.asc(),
            CustomerMaterialSelectionHistory.id.asc(),
        )
    ).all()
    actor_ids = {row.selected_by for row in rows if row.selected_by is not None}
    actors = (
        {
            actor.id: actor
            for actor in db.scalars(select(User).where(User.id.in_(actor_ids))).all()
        }
        if actor_ids
        else {}
    )
    history_items = [
            {
                "id": row.id,
                "candidate_id": row.candidate_id,
                "original_material_code": row.original_material_code_snapshot,
                "original_material_confidence": row.original_material_confidence,
                "original_material_code_snapshot": row.original_material_code_snapshot,
                "selected_material_id": row.selected_material_id,
                "selected_material_code": row.selected_material_code_snapshot,
                "selected_material_code_snapshot": row.selected_material_code_snapshot,
                "supplier_name": row.selected_supplier_name_snapshot,
                "selected_supplier_name_snapshot": row.selected_supplier_name_snapshot,
                "layer_count": row.layer_count_snapshot,
                "flute_type": row.flute_type_snapshot,
                "source_type": row.source_type,
                "source_reference": row.source_reference,
                "selection_reason": row.selection_reason,
                "sync_product": row.sync_product,
                "selected_by": row.selected_by,
                "selected_by_name": (
                    actors[row.selected_by].display_name
                    or actors[row.selected_by].real_name
                    or actors[row.selected_by].username
                )
                if row.selected_by in actors
                else None,
                "selected_at": row.selected_at,
            }
            for row in rows
        ]
    return {
        "item_id": item.id,
        "items": history_items,
        "history": history_items,
    }


@router.get("/material-candidates")
def list_material_candidates(
    customer_id: int | None = Query(default=None, gt=0),
    q: str | None = Query(default=None, max_length=250),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = select(CustomerMaterialCandidate).order_by(
        CustomerMaterialCandidate.customer_id.asc(),
        CustomerMaterialCandidate.original_material_code.asc(),
        CustomerMaterialCandidate.manual_priority.desc(),
        CustomerMaterialCandidate.id.asc(),
    )
    allowed = _allowed_customer_ids(user, db)
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
        query = query.where(CustomerMaterialCandidate.customer_id == customer_id)
    elif allowed is not None:
        query = query.where(CustomerMaterialCandidate.customer_id.in_(allowed))
    if not include_inactive:
        query = query.where(CustomerMaterialCandidate.is_active.is_(True))
    keyword = str(q or "").strip()
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        query = query.where(
            or_(
                CustomerMaterialCandidate.original_material_code.ilike(pattern, escape="\\"),
                CustomerMaterialCandidate.supplier_name.ilike(pattern, escape="\\"),
                CustomerMaterialCandidate.actual_material_code_snapshot.ilike(pattern, escape="\\"),
            )
        )
    candidates = db.scalars(query).all()
    customers = {
        row.id: row
        for row in db.scalars(
            select(Customer).where(
                Customer.id.in_({candidate.customer_id for candidate in candidates})
            )
        ).all()
    } if candidates else {}
    items = []
    for candidate in candidates:
        material = (
            db.get(Material, candidate.actual_material_id)
            if candidate.actual_material_id is not None
            else None
        )
        items.append(
            {
                "id": candidate.id,
                "candidate_id": candidate.id,
                "customer_id": candidate.customer_id,
                "customer_name": customers.get(candidate.customer_id).name
                if customers.get(candidate.customer_id)
                else None,
                "original_material_code": candidate.original_material_code,
                "material_id": candidate.actual_material_id,
                "material_code": material.code
                if material is not None
                else candidate.actual_material_code_snapshot,
                "supplier_name": material.supplier_name
                if material is not None
                else candidate.supplier_name,
                "layer_count": material.layer_count if material is not None else None,
                "manual_priority": candidate.manual_priority,
                "is_active": candidate.is_active,
                "material_is_active": material.is_active if material is not None else False,
                "source": candidate.source,
                "notes": candidate.notes,
                "created_at": candidate.created_at,
                "updated_at": candidate.updated_at,
            }
        )
    return {"items": items, "total": len(items)}


@router.post("/material-candidates", status_code=status.HTTP_201_CREATED)
def create_material_candidate(
    payload: CustomerMaterialCandidateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    customer = db.get(Customer, payload.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=404, detail="客户不存在或已停用")
    require_customer_access(customer.id, user, db)
    material = _candidate_material_or_404(db, payload.material_id)
    _require_active_material_supplier(db, material)
    original_code = payload.original_material_code.strip()
    supplier_name = (material.supplier_name or "未设置供应商").strip()
    candidate = CustomerMaterialCandidate(
        customer_id=customer.id,
        original_material_code=original_code,
        normalized_original_material_code=normalize_material_candidate_key(original_code),
        supplier_name=supplier_name,
        normalized_supplier_name=normalize_supplier_candidate_key(supplier_name),
        actual_material_id=material.id,
        actual_material_code_snapshot=material.code,
        manual_priority=payload.manual_priority,
        is_active=payload.is_active,
        source=payload.source.strip(),
        notes=(payload.notes or "").strip() or None,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(candidate)
    try:
        db.flush()
        _material_candidate_audit(
            db,
            user=user,
            action="CREATE_MATERIAL_CANDIDATE",
            candidate_id=candidate.id,
            details={
                "customer_id": customer.id,
                "original_material_code": original_code,
                "material_id": material.id,
                "material_code": material.code,
            },
            description="新增客户材质候选",
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="该客户材质候选已存在") from exc
    return {"id": candidate.id, "message": "客户材质候选已保存"}


@router.put("/material-candidates/{candidate_id}")
def update_material_candidate(
    candidate_id: int,
    payload: CustomerMaterialCandidateUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    candidate = _candidate_or_404(db, candidate_id)
    require_customer_access(candidate.customer_id, user, db)
    before = {
        "original_material_code": candidate.original_material_code,
        "material_id": candidate.actual_material_id,
        "manual_priority": candidate.manual_priority,
        "is_active": candidate.is_active,
        "source": candidate.source,
        "notes": candidate.notes,
    }
    if "original_material_code" in payload.model_fields_set:
        original_code = str(payload.original_material_code or "").strip()
        candidate.original_material_code = original_code
        candidate.normalized_original_material_code = normalize_material_candidate_key(
            original_code
        )
    if "material_id" in payload.model_fields_set and payload.material_id is not None:
        material = _candidate_material_or_404(db, payload.material_id)
        if material.id != candidate.actual_material_id:
            _require_active_material_supplier(db, material)
        supplier_name = (material.supplier_name or "未设置供应商").strip()
        candidate.actual_material_id = material.id
        candidate.actual_material_code_snapshot = material.code
        candidate.supplier_name = supplier_name
        candidate.normalized_supplier_name = normalize_supplier_candidate_key(
            supplier_name
        )
    for field_name in ("manual_priority", "is_active", "source", "notes"):
        if field_name not in payload.model_fields_set:
            continue
        value = getattr(payload, field_name)
        if field_name in {"source", "notes"}:
            value = str(value or "").strip() or ("manual" if field_name == "source" else None)
        setattr(candidate, field_name, value)
    candidate.updated_by = user.id
    candidate.updated_at = utc_now_naive()
    try:
        db.flush()
        _material_candidate_audit(
            db,
            user=user,
            action="UPDATE_MATERIAL_CANDIDATE",
            candidate_id=candidate.id,
            details={
                "before": before,
                "after": {
                    "original_material_code": candidate.original_material_code,
                    "material_id": candidate.actual_material_id,
                    "manual_priority": candidate.manual_priority,
                    "is_active": candidate.is_active,
                    "source": candidate.source,
                    "notes": candidate.notes,
                },
            },
            description="更新客户材质候选",
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="更新后与现有客户材质候选重复") from exc
    return {"id": candidate.id, "message": "客户材质候选已更新"}


@router.put("/pending/{item_id}/material")
def update_pending_material(
    item_id: int,
    payload: PendingMaterialUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    item = _item_or_404(db, item_id)
    _, customer = _order_customer_for_item(db, item, user)
    if item.requisition_status != "未报料":
        raise HTTPException(status_code=409, detail="已生成报料单的明细不能更换供应商或材质")
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细不能更换供应商或材质")
    if payload.sync_product and not item.product_id:
        raise HTTPException(status_code=409, detail="当前报料明细未关联常用箱")
    material = db.get(Material, payload.material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=404, detail="所选材质不存在或已停用")
    if material.id != item.material_id:
        _require_active_material_supplier(db, material)
    candidate: CustomerMaterialCandidate | None = None
    original_code = (
        item.snapshot_original_material_code or item.snapshot_material or ""
    ).strip()
    original_confidence = (
        "frozen" if (item.snapshot_original_material_code or "").strip() else "legacy_fallback"
    )
    normalized_original = normalize_material_candidate_key(original_code)
    if payload.candidate_id is not None:
        candidate = _candidate_or_404(db, payload.candidate_id)
        if not candidate.is_active:
            raise HTTPException(status_code=409, detail="所选客户材质候选已停用")
        if candidate.customer_id != customer.id:
            raise HTTPException(status_code=400, detail="所选材质候选不属于当前客户")
        if (
            candidate.normalized_original_material_code != normalized_original
            or candidate.actual_material_id != material.id
        ):
            raise HTTPException(status_code=400, detail="候选与当前客户原始材质或所选材质不一致")
    if payload.layer_count is not None and payload.layer_count != material.layer_count:
        raise HTTPException(status_code=400, detail="请求层数与所选材质真实层数不一致")
    layer_count = material.layer_count
    flute_type = (
        payload.flute_type
        if "flute_type" in payload.model_fields_set
        else item.flute_type
    )
    flute_type, flute_error = _business_flute_error(layer_count, flute_type)
    if has_unconsumed_inventory_reservations(db, item.id) and (
        material.id != item.material_id
        or (flute_type or "").strip().upper()
        != (item.flute_type or "").strip().upper()
    ):
        raise HTTPException(
            status_code=409,
            detail="该订单明细已有未消耗库存预占，不能修改材质或楞型；请先释放库存预占。",
        )
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)
    order_values_changed = any(
        (
            material.id != item.material_id,
            material.code != item.snapshot_material,
            material.supplier_name != item.snapshot_supplier_name,
            material.basis_weight_description != item.snapshot_weight,
            layer_count != item.layer_count,
            (flute_type or "").strip().upper()
            != (item.flute_type or "").strip().upper(),
        )
    )
    effective_sync_product = bool(
        item.product_id and (payload.sync_product or order_values_changed)
    )
    product: Product | None = None
    product_updates: dict[str, object] = {}
    product_change_reason: str | None = None
    if effective_sync_product:
        if not has_permission(user, "products.edit"):
            raise HTTPException(status_code=403, detail="缺少 products.edit 权限")
        if payload.product_expected_version is None:
            raise HTTPException(
                status_code=400,
                detail="同步常用箱必须提供 product_expected_version",
            )
        product = db.get(Product, item.product_id)
        if product is None:
            raise HTTPException(status_code=409, detail="关联常用箱不存在，报料明细未保存")
        product_updates = {
            field_name: value
            for field_name, value in {
                "material_id": material.id,
                "layer_count": layer_count,
                "flute_type": flute_type,
            }.items()
            if getattr(product, field_name) != value
        }
        product_change_reason = (payload.product_change_reason or "").strip() or None
    source_reference = (
        (payload.source_reference or "").strip()
        or item.item_order_number
        or f"order-item:{item.id}"
    )
    if not order_values_changed and not product_updates:
        return {
            "item_id": item.id,
            "material_id": material.id,
            "material_code": material.code,
            "supplier_name": material.supplier_name,
            "layer_count": layer_count,
            "flute_type": flute_type,
            "selection_history_id": None,
            "original_material_code": original_code or None,
            "original_material_confidence": original_confidence,
            "message": "材质未变化，无需重复保存",
        }
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
    if product is not None:
        from app.services.master_data_versioning import apply_versioned_update

        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=product_updates,
            expected_version=payload.product_expected_version,
            user=user,
            reason=product_change_reason,
            source=f"requisition.pending-material.sync-product:{source_reference}",
            confirmation_token=payload.product_confirmation_token,
        )
    source_type = str(payload.source_type or "").strip() or (
        "material_candidate" if candidate is not None else "manual"
    )
    history = CustomerMaterialSelectionHistory(
        customer_id=customer.id,
        order_item_id=item.id,
        product_id=item.product_id,
        candidate_id=candidate.id if candidate is not None else None,
        original_material_code_snapshot=original_code or None,
        normalized_original_material_code_snapshot=normalized_original or None,
        original_material_confidence=original_confidence,
        selected_material_id=material.id,
        selected_material_code_snapshot=material.code,
        selected_supplier_name_snapshot=material.supplier_name,
        layer_count_snapshot=layer_count,
        flute_type_snapshot=flute_type,
        source_type=source_type,
        source_reference=source_reference,
        selection_reason=(payload.selection_reason or "").strip() or None,
        sync_product=effective_sync_product,
        selected_by=user.id,
        selected_at=utc_now_naive(),
    )
    db.add(history)
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
                "sync_product": effective_sync_product,
                "candidate_id": candidate.id if candidate is not None else None,
                "source_type": source_type,
                "source_reference": source_reference,
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
        "selection_history_id": history.id,
        "original_material_code": original_code or None,
        "original_material_confidence": original_confidence,
        "message": (
            f"已将该明细改为 {material.supplier_name or '未设置供应商'} / "
            f"{material.code} / {flute_type or '-'}"
        ),
    }


@router.put("/pending/{item_id}/bom-components/{snapshot_id}/material")
def update_pending_bom_component_material(
    item_id: int,
    snapshot_id: int,
    payload: PendingMaterialUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """Update one still-unreported BOM component and its linked common box.

    The parent order item is only the commercial set.  Component material facts
    therefore belong to the frozen component row, not to the parent snapshot.
    """

    item = _item_or_404(db, item_id)
    _, customer = _order_customer_for_item(db, item, user)
    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    if snapshot is None or snapshot.sales_order_item_id != item.id:
        raise HTTPException(status_code=404, detail="订单组件不存在")
    correction_blocker = _bom_snapshot_material_correction_blocker(db, snapshot)
    if correction_blocker:
        raise HTTPException(status_code=409, detail=correction_blocker)

    material = db.get(Material, payload.material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=404, detail="所选材质不存在或已停用")
    _require_active_material_supplier(db, material)
    if payload.candidate_id is not None:
        candidate = _candidate_or_404(db, payload.candidate_id)
        if not candidate.is_active or candidate.customer_id != customer.id:
            raise HTTPException(status_code=400, detail="所选材质候选不属于当前客户")
        if candidate.actual_material_id != material.id:
            raise HTTPException(status_code=400, detail="所选材质候选与材质不一致")
    if payload.layer_count is not None and payload.layer_count != material.layer_count:
        raise HTTPException(status_code=400, detail="请求层数与所选材质真实层数不一致")
    layer_count = material.layer_count
    flute_type = (
        payload.flute_type
        if "flute_type" in payload.model_fields_set
        else snapshot.snapshot_component_flute_type
    )
    flute_type, flute_error = _business_flute_error(layer_count, flute_type)
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)

    product = db.get(Product, snapshot.component_product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=409, detail="组件关联的常用箱不存在或已停用")
    if product.customer_id != customer.id:
        raise HTTPException(status_code=409, detail="组件常用箱与当前订单客户不一致")
    product_updates = {
        field_name: value
        for field_name, value in {
            "material_id": material.id,
            "layer_count": layer_count,
            "flute_type": flute_type,
        }.items()
        if getattr(product, field_name) != value
    }
    component_values_changed = any(
        (
            snapshot.snapshot_component_material_id != material.id,
            snapshot.snapshot_component_material != material.code,
            snapshot.snapshot_component_supplier_name != material.supplier_name,
            snapshot.snapshot_component_layer_count != layer_count,
            (snapshot.snapshot_component_flute_type or "").strip().upper()
            != (flute_type or "").strip().upper(),
        )
    )
    if not component_values_changed and not product_updates:
        return {
            "item_id": item.id,
            "bom_snapshot_id": snapshot.id,
            "material_id": material.id,
            "material_code": material.code,
            "supplier_name": material.supplier_name,
            "layer_count": layer_count,
            "flute_type": flute_type,
            "product_id": product.id,
            "product_version": product.version,
            "message": "组件材质未变化，无需重复保存",
        }
    if product_updates:
        if not has_permission(user, "products.edit"):
            raise HTTPException(status_code=403, detail="缺少 products.edit 权限")
        if payload.product_expected_version is None:
            raise HTTPException(
                status_code=400,
                detail="同步组件常用箱必须提供 product_expected_version",
            )
        from app.services.master_data_versioning import apply_versioned_update

        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=product_updates,
            expected_version=payload.product_expected_version,
            user=user,
            reason=(payload.product_change_reason or "").strip() or None,
            source=(
                "requisition.pending-bom-material.sync-product:"
                f"{item.id}:{snapshot.id}"
            ),
            confirmation_token=payload.product_confirmation_token,
        )

    before = {
        "material_id": snapshot.snapshot_component_material_id,
        "material": snapshot.snapshot_component_material,
        "supplier_name": snapshot.snapshot_component_supplier_name,
        "layer_count": snapshot.snapshot_component_layer_count,
        "flute_type": snapshot.snapshot_component_flute_type,
        "component_product_version": snapshot.component_product_version,
    }
    snapshot.snapshot_component_material_id = material.id
    snapshot.snapshot_component_material = material.code
    snapshot.snapshot_component_supplier_name = material.supplier_name
    snapshot.snapshot_component_layer_count = layer_count
    snapshot.snapshot_component_flute_type = flute_type
    snapshot.component_product_version = product.version
    _audit(
        db,
        user=user,
        action="UPDATE_PENDING_BOM_MATERIAL",
        entity_id=item.id,
        details={
            "bom_snapshot_id": snapshot.id,
            "component_product_id": product.id,
            "before": before,
            "after": {
                "material_id": material.id,
                "material": material.code,
                "supplier_name": material.supplier_name,
                "layer_count": layer_count,
                "flute_type": flute_type,
                "component_product_version": product.version,
            },
        },
        description="更换未报料组件的供应商和材质",
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="组件供应商或材质在保存前已产生业务事实，请刷新后核对",
        ) from error
    return {
        "item_id": item.id,
        "bom_snapshot_id": snapshot.id,
        "material_id": material.id,
        "material_code": material.code,
        "supplier_name": material.supplier_name,
        "layer_count": layer_count,
        "flute_type": flute_type,
        "product_id": product.id,
        "product_version": product.version,
        "message": (
            f"已将组件“{snapshot.snapshot_component_product_name}”改为 "
            f"{material.supplier_name or '未设置供应商'} / {material.code} / "
            f"{flute_type or '-'}"
        ),
    }


@router.put("/pending/{item_id}/virtual-composite-parent")
def mark_pending_composite_parent_virtual(
    item_id: int,
    payload: PendingVirtualCompositeParentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """Convert an untouched set parent to commercial-only parent semantics."""

    if not has_permission(user, "products.edit"):
        raise HTTPException(status_code=403, detail="缺少 products.edit 权限")
    item = _item_or_404(db, item_id)
    _order_customer_for_item(db, item, user)
    product = db.get(Product, item.product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=409, detail="父件常用箱不存在或已停用")
    snapshots = list(
        db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id == item.id)
            .order_by(SalesOrderItemBomComponent.display_order)
        ).all()
    )
    if not snapshots or not bool(getattr(product, "is_composite", False)):
        raise HTTPException(status_code=409, detail="该订单明细不是组合产品父件")
    if (item.combination_mode_snapshot or product.combination_mode) != "parent_priced_set":
        raise HTTPException(status_code=409, detail="只有父件按套计价可以改为只计价父件")

    direct_requisition = db.scalar(
        select(RequisitionItem.id)
        .where(
            RequisitionItem.order_item_id == item.id,
            func.lower(RequisitionItem.status).notin_(
                INACTIVE_REQUISITION_ITEM_STATUSES
            ),
            ~exists().where(
                RequisitionItemBomSource.requisition_item_id
                == RequisitionItem.id
            ),
        )
        .limit(1)
    )
    if direct_requisition is not None:
        raise HTTPException(status_code=409, detail="父件已经正式报料，不能改为只计价父件")
    direct_receipt = db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            IncomingReceiptItem.order_item_id == item.id,
            IncomingReceiptItem.status == "posted",
            or_(
                IncomingReceiptItem.requisition_item_id.is_(None),
                ~exists().where(
                    RequisitionItemBomSource.requisition_item_id
                    == IncomingReceiptItem.requisition_item_id
                ),
            ),
        )
        .limit(1)
    )
    if direct_receipt is not None:
        raise HTTPException(status_code=409, detail="父件已经产生正式收料，不能改为只计价父件")
    parent_reservation = db.scalar(
        select(InventoryReservation.id)
        .where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .limit(1)
    )
    if parent_reservation is not None:
        raise HTTPException(status_code=409, detail="父件已有库存预占，请先释放后再转换")
    parent_task = db.scalar(
        select(ProductionTask.id)
        .where(
            ProductionTask.order_item_id == item.id,
            ProductionTask.sales_order_item_bom_component_id.is_(None),
        )
        .limit(1)
    )
    if parent_task is not None:
        raise HTTPException(status_code=409, detail="父件已有生产任务，不能直接改为只计价父件")

    from app.api.products import _VIRTUAL_COMPOSITE_PARENT_PHYSICAL_FIELDS
    from app.services.master_data_versioning import apply_versioned_update

    product_updates = {
        field_name: None
        for field_name in _VIRTUAL_COMPOSITE_PARENT_PHYSICAL_FIELDS
        if field_name != "die_cut_path"
    }
    product_updates.update(
        {
            "is_virtual_composite_parent": True,
            "box_category": "normal",
            "supply_mode": "corrugated_production",
            "external_packaging_category_code": None,
            "external_packaging_specification_json": None,
            "external_packaging_specification_summary": None,
            "external_packaging_purchase_unit": None,
            "external_packaging_candidate_snapshot_json": None,
            "splice_mode": "single",
            "pieces_per_box": 1,
            "default_cutting_mode": DEFAULT_CUTTING_MODE,
            "printing_plate_mode": "no_plate",
            "printing_plate_1_id": None,
            "printing_plate_2_id": None,
            "printing_plate_3_id": None,
            "plate_alignment_value_mm": None,
            "plate_mount_value_mm": None,
            "machine_set_length_mm": None,
            "machine_set_width_mm": None,
            "machine_set_height_mm": None,
            "production_label_enabled": False,
            "production_label_units_per_label": None,
            "cost_unit_price": None,
            "board_price": None,
            "suggested_price": None,
            "combination_mode": "parent_priced_set",
        }
    )
    apply_versioned_update(
        db,
        object_type="product",
        entity=product,
        updates=product_updates,
        expected_version=payload.product_expected_version,
        user=user,
        reason="组合父件改为只体现整套数量和价格",
        source=f"requisition.pending.virtual-composite-parent:{item.id}",
        confirmation_token=payload.product_confirmation_token,
    )

    item_before = {
        "is_virtual_composite_parent_snapshot": bool(
            item.is_virtual_composite_parent_snapshot
        ),
        "material_id": item.material_id,
        "snapshot_material": item.snapshot_material,
        "snapshot_supplier_name": item.snapshot_supplier_name,
        "layer_count": item.layer_count,
        "flute_type": item.flute_type,
    }
    item.is_virtual_composite_parent_snapshot = True
    item.material_id = None
    item.snapshot_material = None
    item.snapshot_original_material_code = None
    item.snapshot_production_notes = None
    item.snapshot_supplier_name = None
    item.snapshot_weight = None
    item.layer_count = None
    item.flute_type = None
    item.drawing_file = None
    item.requisition_qty = None
    item.requisition_spec = None
    item.cardboard_len = None
    item.cardboard_width = None
    item.requisition_date = None
    item.supplier_delivery_time = None
    item.supplier_order_number = None
    item.requisition_remark = None
    item.special_process = DEFAULT_CUTTING_MODE
    for field_name in (
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
    ):
        setattr(item, field_name, None)
    _audit(
        db,
        user=user,
        action="MARK_PENDING_PARENT_VIRTUAL",
        entity_id=item.id,
        details={
            "product_id": product.id,
            "product_version": product.version,
            "before": item_before,
            "bom_snapshot_ids": [snapshot.id for snapshot in snapshots],
        },
        description="将未产生业务事实的组合父件改为只计价父件",
    )
    db.commit()
    return {
        "item_id": item.id,
        "product_id": product.id,
        "product_version": product.version,
        "is_virtual_composite_parent": True,
        "message": "父件已改为只体现整套数量和价格；请分别核对实体组件供应商和材质",
    }


@router.get("/items")
def list_requisition_items(
    status_filter: str | None = Query(default=None, alias="status"),
    include_history: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    user = _user
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
    allowed = _allowed_customer_ids(user, db)
    if allowed is not None:
        query = query.where(Order.customer_id.in_(allowed))
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


def _confirmed_composite_requisition_qty(
    line: RequisitionLinePayload,
    minimum_quantity: int,
) -> int:
    """Honor the reviewed draft quantity without allowing a hidden shortage."""
    requested = (
        minimum_quantity
        if line.requisition_qty is None
        else int(line.requisition_qty)
    )
    if requested < minimum_quantity:
        raise HTTPException(
            status_code=400,
            detail=(
                f"本次采购张数不能少于库存抵扣后的系统最低 "
                f"{minimum_quantity} 张；如需减少，请先在报料明细中使用匹配库存"
            ),
        )
    return requested


@router.post("/batches", status_code=status.HTTP_201_CREATED)
def create_batch(
    payload: RequisitionBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    requisition_date = beijing_today()
    try:
        supplier_name = (payload.supplier_name or "").strip()
        if supplier_name:
            supplier_name = _require_active_supplier(db, supplier_name)
        batch = Requisition(
            requisition_number=_next_number(db, requisition_date),
            requisition_date=requisition_date,
            supplier_name=supplier_name or None,
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
                select(OrderItem, Product, Order)
                .join(Product, Product.id == OrderItem.product_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(OrderItem.id == order_item_id)
                .with_for_update()
            ).one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="订单明细不存在")
            item, product, order = row
            _require_order_item_customer_access(db, item, user)
            if _active_requisition_hold(db, item.id) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="订单明细正在等候报料，请先恢复到待报料",
                )
            selected_material = (
                db.get(Material, item.material_id)
                if item.material_id is not None
                else None
            )
            _require_active_material_supplier(
                db,
                selected_material,
                detail_prefix=f"订单明细 {item.id}：",
            )
            if selected_material is None and (item.snapshot_supplier_name or "").strip():
                _require_active_supplier(
                    db,
                    item.snapshot_supplier_name,
                    detail_prefix=f"订单明细 {item.id}：",
                )
            _require_order_item_customer_access(db, item, user)
            _require_late_finished_inventory_resolved(
                db,
                item=item,
                order=order,
                product=product,
            )
            bom_snapshots = _bom_snapshots_for_order_item(db, item.id)
            if bom_snapshots:
                suppress_parent_requisition = (
                    _composite_parent_requisition_is_suppressed(
                        item,
                        bom_snapshots,
                    )
                )
                if item.material_status == "received":
                    raise HTTPException(
                        status_code=409,
                        detail="已入库明细不能继续创建组件报料",
                    )
                parent_lines = [
                    line for line in lines if line.bom_snapshot_id is None
                ]
                if len(parent_lines) > 1:
                    raise HTTPException(
                        status_code=400,
                        detail="复合产品父件报料明细不能重复",
                    )
                if suppress_parent_requisition and parent_lines:
                    raise HTTPException(
                        status_code=400,
                        detail="该组合父档只表示一套，不是第4条纸板，不能生成父件报料",
                    )
                parent_already_requisitioned = (
                    suppress_parent_requisition
                    or _bom_parent_has_active_requisition(
                        db,
                        item.id,
                    )
                )
                if not parent_already_requisitioned and not parent_lines:
                    raise HTTPException(
                        status_code=400,
                        detail="复合产品报料必须同时包含父件外包装盒",
                    )
                if parent_already_requisitioned and parent_lines:
                    raise HTTPException(
                        status_code=409,
                        detail="该复合产品父件已经报料，不能重复创建",
                    )
                selected_snapshot_by_id = {
                    snapshot.id: snapshot for snapshot in bom_snapshots
                }
                selected_source_keys: set[tuple[int, str]] = set()
                for line in lines:
                    if line.bom_snapshot_id is None:
                        continue
                    selected_snapshot = selected_snapshot_by_id.get(
                        int(line.bom_snapshot_id)
                    )
                    if selected_snapshot is None:
                        raise HTTPException(
                            status_code=400,
                            detail="所选复合产品组件不属于当前订单明细",
                        )
                    selected_source_keys.add(
                        (
                            selected_snapshot.id,
                            _bom_snapshot_component_type(
                                selected_snapshot,
                                line.component_type,
                            ),
                        )
                    )
                pending_source_keys = {
                    (snapshot.id, component_type)
                    for snapshot in bom_snapshots
                    for component_type in _bom_snapshot_component_types(snapshot)
                    if not _bom_snapshot_has_active_requisition(
                        db,
                        snapshot.id,
                        component_type=component_type,
                    )
                    and int(
                        _bom_snapshot_requirements(
                            db,
                            snapshot,
                            component_type=component_type,
                        )["remaining_required_piece_qty"]
                    )
                    > 0
                }
                selected_active_source = any(
                    _bom_snapshot_has_active_requisition(
                        db,
                        snapshot_id,
                        component_type=component_type,
                    )
                    for snapshot_id, component_type in selected_source_keys
                )
                if (
                    pending_source_keys
                    and not (pending_source_keys & selected_source_keys)
                    and not selected_active_source
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="复合产品报料必须同时包含至少一个待报料组件",
                    )
                for line in lines:
                    if line.bom_snapshot_id is None:
                        if line.inventory_deducted_qty:
                            raise HTTPException(
                                status_code=400,
                                detail="旧库存抵扣字段已停用，请使用真实成品库存预占",
                            )
                        parent_requirements = _current_requisition_requirements(
                            db,
                            item,
                            cutting_mode=line.special_process,
                            pieces_per_box=1,
                        )
                        if int(parent_requirements["remaining_required_piece_qty"]) <= 0:
                            raise HTTPException(
                                status_code=409,
                                detail="该复合产品父件已由库存全额抵扣，无需报料",
                            )
                        parent_minimum_qty = int(
                            parent_requirements["requisition_qty"]
                        )
                        parent_confirmed_qty = (
                            _confirmed_composite_requisition_qty(
                                line,
                                parent_minimum_qty,
                            )
                        )
                        batch_item = RequisitionItem(
                            requisition_id=batch.id,
                            order_item_id=item.id,
                            inventory_deducted_qty=0,
                            requisition_qty=parent_confirmed_qty,
                            cardboard_len=line.cardboard_len,
                            cardboard_width=line.cardboard_width,
                            pieces_per_box=1,
                            required_piece_qty=int(
                                parent_requirements["required_piece_qty"]
                            ),
                            special_process=str(
                                parent_requirements["cutting_mode"]
                            ),
                            material_snapshot=item.snapshot_material,
                            product_code_snapshot=item.snapshot_product_code,
                            product_name_snapshot=item.snapshot_product_name,
                            specification_snapshot=item.snapshot_spec,
                            remark=(line.remark or "").strip() or None,
                            status="有效",
                        )
                        db.add(batch_item)
                        item.special_process = str(
                            parent_requirements["cutting_mode"]
                        )
                        item.cardboard_len = line.cardboard_len
                        item.cardboard_width = line.cardboard_width
                        continue
                    snapshot = db.get(
                        SalesOrderItemBomComponent,
                        line.bom_snapshot_id,
                    )
                    if (
                        snapshot is None
                        or snapshot.sales_order_item_id != item.id
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="所选复合产品组件不属于当前订单明细",
                        )
                    if (snapshot.snapshot_component_supplier_name or "").strip():
                        _require_active_supplier(
                            db,
                            snapshot.snapshot_component_supplier_name,
                            detail_prefix=(
                                f"组件“{snapshot.snapshot_component_product_name}”："
                            ),
                        )
                    component_type = _bom_snapshot_component_type(
                        snapshot,
                        line.component_type,
                    )
                    if _bom_snapshot_has_active_requisition(
                        db,
                        snapshot.id,
                        component_type=component_type,
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="该复合产品物理料已经报料，不能重复创建",
                        )
                    if line.actual_yield_per_sheet is not None and not snapshot.is_die_cut:
                        raise HTTPException(
                            status_code=400,
                            detail="非模切组件不能填写实际模切出数",
                        )
                    requirements = _bom_snapshot_requirements(
                        db,
                        snapshot,
                        component_type=component_type,
                        cutting_mode=line.special_process,
                        actual_yield_per_sheet=line.actual_yield_per_sheet,
                    )
                    if int(requirements["remaining_required_piece_qty"]) <= 0:
                        raise HTTPException(
                            status_code=409,
                            detail="该复合产品组件已由半成品库存全额抵扣，无需报料",
                        )
                    component_minimum_qty = int(
                        requirements["requisition_qty"]
                    )
                    component_confirmed_qty = (
                        _confirmed_composite_requisition_qty(
                            line,
                            component_minimum_qty,
                        )
                    )
                    cardboard_len = Decimal(
                        requirements["report_length_mm"]
                        or line.cardboard_len
                    )
                    cardboard_width = Decimal(
                        requirements["report_width_mm"]
                        or line.cardboard_width
                    )
                    component_remark = " / ".join(
                        value
                        for value in [
                            (line.remark or "").strip(),
                            (requirements.get("remark") or "").strip(),
                        ]
                        if value
                    ) or None
                    batch_item = RequisitionItem(
                        requisition_id=batch.id,
                        order_item_id=item.id,
                        inventory_deducted_qty=0,
                        requisition_qty=component_confirmed_qty,
                        cardboard_len=cardboard_len,
                        cardboard_width=cardboard_width,
                        pieces_per_box=int(
                            requirements["physical_pieces_per_component"]
                        ),
                        required_piece_qty=int(
                            requirements["required_piece_quantity"]
                        ),
                        special_process=str(requirements["cutting_mode"]),
                        material_snapshot=snapshot.snapshot_component_material,
                        product_code_snapshot=snapshot.snapshot_component_product_code,
                        product_name_snapshot=str(requirements["product_name"]),
                        specification_snapshot=snapshot.snapshot_component_spec,
                        remark=component_remark,
                        status="有效",
                    )
                    db.add(batch_item)
                    db.flush()
                    db.add(
                        RequisitionItemBomSource(
                            requisition_item_id=batch_item.id,
                            sales_order_item_bom_component_id=snapshot.id,
                            component_type=component_type,
                            active_guard=1,
                            order_set_quantity=int(
                                requirements["effective_set_quantity"]
                            ),
                            quantity_per_set=Decimal(
                                requirements["quantity_per_set"]
                            )
                            * Decimal(
                                requirements["physical_pieces_per_component"]
                            ),
                            required_piece_quantity=Decimal(
                                requirements["required_piece_quantity"]
                            ),
                            demand_basis=(
                                "order_specific_pieces"
                                if int(requirements["required_piece_quantity"])
                                != int(requirements["effective_set_quantity"])
                                * int(requirements["quantity_per_set"])
                                * int(
                                    requirements[
                                        "physical_pieces_per_component"
                                    ]
                                )
                                else "order_sets"
                            ),
                            mold_max_yield_per_sheet=snapshot.mold_max_yield_per_sheet,
                            actual_yield_per_sheet=(
                                Decimal(line.actual_yield_per_sheet)
                                if line.actual_yield_per_sheet is not None
                                else None
                            ),
                            spare_sheet_quantity=int(
                                requirements["spare_sheet_quantity"]
                            ),
                            calculated_purchase_quantity=Decimal(
                                component_minimum_qty
                            ),
                            direction_note=(
                                f"剩余需求片数：{requirements['remaining_required_piece_qty']}；"
                                f"系统最低报料：{component_minimum_qty}；"
                                f"本次确认报料：{component_confirmed_qty}"
                            ),
                            calculation_rule_version=(
                                "bom-physical-source-v3"
                                if (
                                    component_type != "whole"
                                    or int(
                                        requirements[
                                            "physical_pieces_per_component"
                                        ]
                                    )
                                    != 1
                                )
                                else "bom-demand-cutting-v2"
                            ),
                        )
                    )
                db.flush()
                item.inventory_deducted_qty = 0
                item.requisition_qty = int(
                    db.scalar(
                        select(func.coalesce(func.sum(RequisitionItem.requisition_qty), 0))
                        .where(
                            RequisitionItem.order_item_id == item.id,
                            func.lower(RequisitionItem.status).notin_(
                                INACTIVE_REQUISITION_ITEM_STATUSES
                            ),
                        )
                    )
                    or 0
                )
                item.requisition_status = (
                    "已报料"
                    if _bom_order_item_is_fully_requisitioned(
                        db,
                        item,
                        bom_snapshots,
                    )
                    else "未报料"
                )
                item.requisition_date = requisition_date
                item.requisition_remark = (lines[0].remark or "").strip() or None
                response_items.append(_item_response(item, db))
                continue
            if any(line.bom_snapshot_id is not None for line in lines):
                raise HTTPException(
                    status_code=400,
                    detail="当前订单明细不是复合产品，不能传 bom_snapshot_id",
                )
            if item.material_status == "received":
                raise HTTPException(status_code=409, detail="已入库明细不能报料")
            if item.requisition_status != "未报料":
                raise HTTPException(status_code=409, detail="订单明细已经报料")
            _ensure_order_item_crease_width(item)
            base_requirements = _current_requisition_requirements(db, item)
            production_required_qty = int(
                base_requirements["production_required_qty"]
            )
            if production_required_qty == 0:
                raise HTTPException(
                    status_code=409,
                    detail="该订单明细已由成品库存全额抵扣，无需报料",
                )
            components = []
            for line in lines:
                if line.inventory_deducted_qty:
                    raise HTTPException(
                        status_code=400,
                        detail="旧库存抵扣字段已停用，请在订单明细中选择真实成品库存预占",
                    )
                component_types = (
                    [line.component_type]
                    if line.component_type
                    else (
                        ["cover", "base"]
                        if _is_telescoping_lid_box(product.box_style)
                        else ["whole"]
                    )
                )
                for component_type in component_types:
                    is_base = component_type == "base"
                    requirements = _current_requisition_requirements(
                        db,
                        item,
                        cutting_mode=line.special_process,
                        component_type=component_type,
                    )
                    components.append(
                        {
                            "kind": component_type,
                            "suffix": (
                                "底"
                                if is_base
                                else "盖" if component_type == "cover" else ""
                            ),
                            "cardboard_len": (
                                Decimal(item.snapshot_base_report_length_mm)
                                if is_base and item.snapshot_base_report_length_mm
                                else line.cardboard_len
                            ),
                            "cardboard_width": (
                                Decimal(item.snapshot_base_report_width_mm)
                                if is_base and item.snapshot_base_report_width_mm
                                else line.cardboard_width
                            ),
                            "requisition_qty": int(requirements["requisition_qty"]),
                            "pieces_per_box": int(requirements["pieces_per_box"]),
                            "required_piece_qty": int(requirements["required_piece_qty"]),
                            "semi_finished_reserved_piece_qty": int(
                                requirements["semi_finished_reserved_piece_qty"]
                            ),
                            "remaining_required_piece_qty": int(
                                requirements["remaining_required_piece_qty"]
                            ),
                            "report_notes": (
                                item.snapshot_base_report_notes
                                if is_base
                                else item.snapshot_report_notes
                            ),
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
                    pieces_per_box=int(component["pieces_per_box"]),
                    required_piece_qty=int(component["required_piece_qty"]),
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
    except IntegrityError as error:
        db.rollback()
        message = str(getattr(error, "orig", error)).lower()
        if (
            "uq_requisition_item_bom_sources_active_physical_source"
            in message
            or (
                "requisition_item_bom_sources"
                in message
                and "sales_order_item_bom_component_id" in message
                and "component_type" in message
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="该盖片、底片或围板已经报料，请刷新后重试",
            ) from error
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
    _require_order_item_customer_access(db, item, user)
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细禁止修改报料")
    if item.requisition_status == "未报料":
        raise HTTPException(status_code=409, detail="该明细尚未报料")
    if payload.inventory_deducted_qty:
        raise HTTPException(
            status_code=400,
            detail="旧库存抵扣字段已停用，真实抵扣只能来自成品库存预占",
        )
    item.inventory_deducted_qty = 0
    item.cardboard_len = payload.cardboard_len
    item.cardboard_width = payload.cardboard_width
    item.requisition_spec = f"{_plain(payload.cardboard_len)}?{_plain(payload.cardboard_width)}"
    item.special_process = payload.special_process
    item.requisition_remark = (payload.remark or "").strip() or None
    requisition_items = db.scalars(
        select(RequisitionItem).where(
            RequisitionItem.order_item_id == item.id,
            RequisitionItem.status == "有效",
        )
    ).all()
    total_requisition_qty = 0
    for requisition_item in requisition_items:
        product_name = requisition_item.product_name_snapshot or ""
        component_type = (
            "base"
            if product_name.endswith("-底")
            else "cover" if product_name.endswith("-盖") else "whole"
        )
        requirements = _current_requisition_requirements(
            db,
            item,
            cutting_mode=payload.special_process,
            pieces_per_box=(
                requisition_item.pieces_per_box or _pieces_per_box(item)
            ),
            component_type=component_type,
        )
        requisition_item.inventory_deducted_qty = 0
        requisition_item.requisition_qty = int(requirements["requisition_qty"])
        requisition_item.cardboard_len = payload.cardboard_len
        requisition_item.cardboard_width = payload.cardboard_width
        requisition_item.pieces_per_box = int(requirements["pieces_per_box"])
        requisition_item.required_piece_qty = int(
            requirements["required_piece_qty"]
        )
        requisition_item.special_process = payload.special_process
        requisition_item.remark = item.requisition_remark
        total_requisition_qty += requisition_item.requisition_qty
    item.requisition_qty = total_requisition_qty
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
    _require_order_item_customer_access(db, item, user)
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
    user: User = Depends(admin_rollback),
) -> dict:
    reason = (payload.reason or "").strip() or "取消报料并退回待报料（系统记录）"
    item = _item_or_404(db, item_id)
    _require_order_item_customer_access(db, item, user)
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细禁止修改报料")
    if item.requisition_status == "未报料":
        raise HTTPException(status_code=409, detail="订单明细已经回到待报料，不能重复撤销")
    has_posted_receipt = db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            IncomingReceiptItem.order_item_id == item.id,
            IncomingReceiptItem.status == "posted",
        )
        .limit(1)
    )
    if has_posted_receipt is not None:
        raise HTTPException(
            status_code=409,
            detail="该订单已有实际收货，必须先撤销来料实收",
        )
    before_requisition = {
        "requisition_status": item.requisition_status,
        "requisition_qty": item.requisition_qty,
        "inventory_deducted_qty": int(item.inventory_deducted_qty or 0),
        "supplier_order_number": item.supplier_order_number,
    }
    affected_batch_ids = sorted(
        {
            int(batch_id)
            for batch_id in db.scalars(
                select(RequisitionItem.requisition_id).where(
                    RequisitionItem.order_item_id == item.id,
                    RequisitionItem.status == "有效",
                )
            ).all()
        }
    )
    db.execute(
        update(RequisitionItem)
        .where(
            RequisitionItem.order_item_id == item.id,
            RequisitionItem.status == "有效",
        )
        .values(status="已取消")
    )
    db.execute(
        update(RequisitionItemBomSource)
        .where(
            RequisitionItemBomSource.requisition_item_id.in_(
                select(RequisitionItem.id).where(
                    RequisitionItem.order_item_id == item.id
                )
            )
        )
        .values(active_guard=None)
    )
    cancelled_batch_ids: list[int] = []
    for batch_id in affected_batch_ids:
        active_line_id = db.scalar(
            select(RequisitionItem.id)
            .where(
                RequisitionItem.requisition_id == batch_id,
                func.lower(RequisitionItem.status).notin_(
                    INACTIVE_REQUISITION_ITEM_STATUSES
                ),
            )
            .limit(1)
        )
        if active_line_id is not None:
            continue
        batch = db.get(Requisition, batch_id)
        if batch is None or batch.status in INACTIVE_REQUISITION_ITEM_STATUSES:
            continue
        batch.status = "已取消"
        cancelled_batch_ids.append(batch_id)
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
    try:
        release_active_finished_reservations_for_items(
            db,
            order_item_ids=[item.id],
            operator_id=user.id,
            reason="撤回报料，自动释放成品库存预占",
            idempotency_prefix=f"cancel-requisition-{item.id}-finished",
        )
        release_active_semi_reservations_for_items(
            db,
            order_item_ids=[item.id],
            operator_id=user.id,
            reason="撤回报料，自动释放半成品库存预占",
            idempotency_prefix=f"cancel-requisition-{item.id}-semi",
        )
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    _audit(
        db,
        user=user,
        action="CANCEL_REQUISITION",
        entity_id=item.id,
        details={
            "reason": reason,
            "cancelled_requisition_batch_ids": cancelled_batch_ids,
            "before": before_requisition,
            "after": {
                "requisition_status": item.requisition_status,
                "requisition_qty": item.requisition_qty,
                "inventory_deducted_qty": int(item.inventory_deducted_qty or 0),
                "supplier_order_number": item.supplier_order_number,
            },
        },
        description="管理员撤销报料并回到待报料",
    )
    db.commit()
    return _item_response(item, db)


def _refresh_bom_order_item_requisition_state(
    db: Session,
    item: OrderItem,
) -> None:
    snapshots = _bom_snapshots_for_order_item(db, item.id)
    active_total = int(
        db.scalar(
            select(func.coalesce(func.sum(RequisitionItem.requisition_qty), 0))
            .where(
                RequisitionItem.order_item_id == item.id,
                func.lower(RequisitionItem.status).notin_(
                    INACTIVE_REQUISITION_ITEM_STATUSES
                ),
            )
        )
        or 0
    )
    item.requisition_qty = active_total or None
    item.requisition_status = (
        "已报料"
        if snapshots
        and _bom_order_item_is_fully_requisitioned(
            db,
            item,
            snapshots,
        )
        else "未报料"
    )
    if active_total == 0:
        item.requisition_date = None
        item.supplier_delivery_time = None
        item.supplier_order_number = None
        item.requisition_remark = None


@router.put("/batch-items/{requisition_item_id}/void")
def void_composite_requisition_item(
    requisition_item_id: int,
    payload: CancelPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """撤销一个组合 BOM 物理料来源，不影响同订单其他盖/底/围板。"""
    row = db.get(RequisitionItem, requisition_item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    item = _item_or_404(db, row.order_item_id)
    _require_order_item_customer_access(db, item, user)
    source = db.scalar(
        select(RequisitionItemBomSource).where(
            RequisitionItemBomSource.requisition_item_id == row.id
        )
    )
    if source is None:
        raise HTTPException(
            status_code=409,
            detail="只有组合 BOM 物理料明细可逐条撤销",
        )
    if row.status in {"已取消", "已作废", "已撤回"}:
        return {
            "requisition_item_id": row.id,
            "status": row.status,
            "already_voided": True,
            "order_item_id": item.id,
            "requisition_status": item.requisition_status,
        }
    received_fact = db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            IncomingReceiptItem.requisition_item_id == row.id,
            IncomingReceiptItem.status == "posted",
        )
        .limit(1)
    )
    if row.status == "已入库" or received_fact is not None:
        raise HTTPException(
            status_code=409,
            detail="该报料明细已有实际收货，必须先撤销来料实收",
        )
    if row.status != "有效":
        raise HTTPException(
            status_code=409,
            detail="该报料明细已进入供应商排单或其他后续流程，不能直接撤销",
        )

    row.status = "已取消"
    source.active_guard = None
    db.flush()
    _refresh_bom_order_item_requisition_state(db, item)
    batch = db.get(Requisition, row.requisition_id)
    if batch is not None:
        active_in_batch = int(
            db.scalar(
                select(func.count(RequisitionItem.id)).where(
                    RequisitionItem.requisition_id == batch.id,
                    func.lower(RequisitionItem.status).notin_(
                        INACTIVE_REQUISITION_ITEM_STATUSES
                    ),
                )
            )
            or 0
        )
        if active_in_batch == 0:
            batch.status = "已取消"
    db.add(
        OperationLog(
            user_id=user.id,
            action="VOID_COMPOSITE_REQUISITION_ITEM",
            resource="RequisitionItem",
            details=json.dumps(
                {
                    "requisition_item_id": row.id,
                    "requisition_id": row.requisition_id,
                    "order_item_id": item.id,
                    "snapshot_id": (
                        source.sales_order_item_bom_component_id
                    ),
                    "component_type": source.component_type,
                    "reason": payload.reason,
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="requisition_item",
            entity_id=row.id,
            description="逐条撤销组合 BOM 物理料报料",
        )
    )
    db.commit()
    return {
        "requisition_item_id": row.id,
        "status": row.status,
        "already_voided": False,
        "order_item_id": item.id,
        "requisition_status": item.requisition_status,
        "source_key": (
            f"component:{source.sales_order_item_bom_component_id}:"
            f"{source.component_type}"
        ),
    }


@router.put("/batches/{batch_id}/void")
def void_composite_requisition_batch(
    batch_id: int,
    payload: CancelPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    batch = db.scalar(
        select(Requisition)
        .options(selectinload(Requisition.items))
        .where(Requisition.id == batch_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="报料单不存在")
    _require_requisition_customer_access(batch, user, db)
    requisition_item_ids = [row.id for row in batch.items]
    has_bom_source = bool(
        requisition_item_ids
        and db.scalar(
            select(RequisitionItemBomSource.id)
            .where(
                RequisitionItemBomSource.requisition_item_id.in_(
                    requisition_item_ids
                )
            )
            .limit(1)
        )
    )
    if not has_bom_source:
        raise HTTPException(
            status_code=409,
            detail="只有组合 BOM 报料单可在此整单作废",
        )
    if batch.status == "已取消":
        return {
            "id": batch.id,
            "status": batch.status,
            "voided_item_count": len(batch.items),
        }
    if batch.status != "已报料" or any(
        row.status != "有效" for row in batch.items
    ):
        raise HTTPException(
            status_code=409,
            detail="该组合报料单已进入排单或入库流程，不能直接作废；请先走对应撤销流程。",
        )
    posted_receipt = db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            IncomingReceiptItem.requisition_item_id.in_(
                requisition_item_ids
            ),
            IncomingReceiptItem.status == "posted",
        )
        .limit(1)
    )
    if posted_receipt is not None:
        raise HTTPException(
            status_code=409,
            detail="该组合报料单已有实际收货，必须先撤销来料实收",
        )
    order_item_ids = sorted({row.order_item_id for row in batch.items})
    order_items = db.scalars(
        select(OrderItem).where(OrderItem.id.in_(order_item_ids))
    ).all()
    if any(item.material_status == "received" for item in order_items):
        raise HTTPException(
            status_code=409,
            detail="该组合报料单已有入库事实，不能直接作废。",
        )

    batch.status = "已取消"
    for row in batch.items:
        row.status = "已取消"
    db.execute(
        update(RequisitionItemBomSource)
        .where(
            RequisitionItemBomSource.requisition_item_id.in_(
                requisition_item_ids
            )
        )
        .values(active_guard=None)
    )
    db.flush()
    for item in order_items:
        snapshots = _bom_snapshots_for_order_item(db, item.id)
        if not snapshots:
            continue
        active_total = int(
            db.scalar(
                select(func.coalesce(func.sum(RequisitionItem.requisition_qty), 0))
                .where(
                    RequisitionItem.order_item_id == item.id,
                    func.lower(RequisitionItem.status).notin_(
                        INACTIVE_REQUISITION_ITEM_STATUSES
                    ),
                )
            )
            or 0
        )
        item.requisition_qty = active_total or None
        item.requisition_status = (
            "已报料"
            if _bom_order_item_is_fully_requisitioned(
                db,
                item,
                snapshots,
            )
            else "未报料"
        )
        if item.requisition_status == "未报料":
            item.requisition_date = None
            item.supplier_delivery_time = None
            item.supplier_order_number = None
            item.requisition_remark = None
    db.add(
        OperationLog(
            user_id=user.id,
            action="VOID_COMPOSITE_REQUISITION",
            resource="Requisition",
            details=json.dumps(
                {
                    "batch_id": batch.id,
                    "reason": payload.reason.strip(),
                    "order_item_ids": order_item_ids,
                    "requisition_item_ids": requisition_item_ids,
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="requisition",
            entity_id=batch.id,
            description="作废组合 BOM 报料单并退回待报料池",
        )
    )
    db.commit()
    return {
        "id": batch.id,
        "status": batch.status,
        "voided_item_count": len(batch.items),
    }


def _stock_policy_query():
    return select(InventoryStockPolicy).options(
        selectinload(InventoryStockPolicy.product).selectinload(Product.material),
        selectinload(InventoryStockPolicy.customer),
        selectinload(InventoryStockPolicy.default_location),
    )


def _active_finished_stock_policies(
    db: Session,
    *,
    product_id: int,
    exclude_policy_id: int | None = None,
) -> list[InventoryStockPolicy]:
    query = _stock_policy_query().where(
        InventoryStockPolicy.target_inventory_type == "finished",
        InventoryStockPolicy.product_id == product_id,
        InventoryStockPolicy.active.is_(True),
    )
    if exclude_policy_id is not None:
        query = query.where(InventoryStockPolicy.id != exclude_policy_id)
    return db.scalars(query.order_by(InventoryStockPolicy.id)).all()


def _stock_policy_customer_id(
    db: Session,
    policy: InventoryStockPolicy,
    *,
    relationships_loaded: bool = False,
) -> int | None:
    if policy.customer_id is None:
        return None
    customer_ids = {policy.customer_id}
    if policy.product_id is not None:
        product = (
            policy.product
            if relationships_loaded
            else db.get(Product, policy.product_id)
        )
        if product is None or product.deleted_at is not None:
            return None
        customer_ids.add(product.customer_id)
    return next(iter(customer_ids)) if len(customer_ids) == 1 else None


def _require_stock_policy_customer_access(
    db: Session, policy: InventoryStockPolicy, user: User
) -> None:
    allowed = _allowed_customer_ids(user, db)
    if allowed is None:
        return
    customer_id = _stock_policy_customer_id(db, policy)
    if customer_id is None or customer_id not in allowed:
        raise HTTPException(status_code=403, detail="无客户库存策略访问权限")


def _apply_stock_policy_scope(query, user: User, db: Session):
    allowed = _allowed_customer_ids(user, db)
    if allowed is None:
        return query
    matching_product = exists(
        select(Product.id).where(
            Product.id == InventoryStockPolicy.product_id,
            Product.customer_id == InventoryStockPolicy.customer_id,
            InventoryStockPolicy.customer_id.in_(allowed),
            Product.customer_id.in_(allowed),
            Product.deleted_at.is_(None),
        )
    )
    return query.where(
        or_(
            and_(
                InventoryStockPolicy.product_id.is_(None),
                InventoryStockPolicy.customer_id.in_(allowed),
            ),
            matching_product,
        )
    )


def _stock_policy_summary(
    db: Session,
    policy: InventoryStockPolicy,
    user: User,
) -> dict:
    return stock_policy_dict(db, policy)


def _stock_replenishment_item_customer_id(
    db: Session,
    item: StockReplenishmentOrderItem,
    *,
    relationships_loaded: bool = False,
) -> int | None:
    customer_ids: set[int] = set()
    if item.customer_id is not None:
        customer_ids.add(item.customer_id)
    if item.product_id is not None:
        product = (
            item.product
            if relationships_loaded
            else db.get(Product, item.product_id)
        )
        if product is None or product.deleted_at is not None:
            return None
        customer_ids.add(product.customer_id)
    if item.stock_policy_id is not None:
        policy = (
            item.stock_policy
            if relationships_loaded
            else db.get(InventoryStockPolicy, item.stock_policy_id)
        )
        if policy is None:
            return None
        policy_customer_id = _stock_policy_customer_id(
            db,
            policy,
            relationships_loaded=relationships_loaded,
        )
        if policy_customer_id is None:
            return None
        customer_ids.add(policy_customer_id)
    return next(iter(customer_ids)) if len(customer_ids) == 1 else None


def _stock_replenishment_order_is_visible(
    db: Session,
    order: StockReplenishmentOrder,
    user: User,
    *,
    relationships_loaded: bool = False,
) -> bool:
    allowed = _allowed_customer_ids(user, db)
    if allowed is None:
        return True
    if not order.items:
        return False
    item_customer_ids = {
        _stock_replenishment_item_customer_id(
            db,
            item,
            relationships_loaded=relationships_loaded,
        )
        for item in order.items
    }
    if None in item_customer_ids or not item_customer_ids.issubset(allowed):
        return False
    if order.customer_id is not None and (
        order.customer_id not in allowed
        or order.customer_id not in item_customer_ids
    ):
        return False
    return True


def _require_stock_replenishment_order_access(
    db: Session,
    order: StockReplenishmentOrder,
    user: User,
    *,
    relationships_loaded: bool = False,
) -> None:
    if not _stock_replenishment_order_is_visible(
        db,
        order,
        user,
        relationships_loaded=relationships_loaded,
    ):
        raise HTTPException(status_code=403, detail="无客户补库单访问权限")


def _apply_stock_policy_payload(
    row: InventoryStockPolicy,
    payload: StockPolicyPayload,
    *,
    user_id: int,
) -> None:
    values = payload.model_dump()
    material_code = (values.pop("material_code", None) or "").strip() or None
    for key, value in values.items():
        setattr(row, key, value)
    row.material_code_snapshot = material_code
    row.normalized_material_code = (
        normalize_material_code(material_code) if material_code else None
    )
    row.updated_by = user_id


@router.get("/stock-policies")
def list_stock_policies(
    q: str | None = None,
    product_id: int | None = None,
    target_inventory_type: str | None = None,
    warning_only: bool = False,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = _apply_stock_policy_scope(_stock_policy_query(), _user, db)
    if not include_inactive:
        query = query.where(InventoryStockPolicy.active.is_(True))
    if product_id is not None:
        query = query.where(InventoryStockPolicy.product_id == product_id)
    if target_inventory_type:
        query = query.where(
            InventoryStockPolicy.target_inventory_type
            == target_inventory_type.strip().lower()
        )
    rows = db.scalars(query.order_by(InventoryStockPolicy.id.desc())).all()
    items = [_stock_policy_summary(db, row, _user) for row in rows]
    if q and q.strip():
        keyword = q.strip().lower()
        items = [
            item
            for item in items
            if keyword
            in " ".join(
                str(item.get(key) or "")
                for key in (
                    "policy_name",
                    "customer_name",
                    "product_code",
                    "product_name",
                    "material_code",
                    "supplier_name",
                )
            ).lower()
        ]
    if warning_only:
        items = [item for item in items if item["warning_triggered"]]
    return {
        "items": items,
        "warning_count": sum(1 for item in items if item["warning_triggered"]),
    }


def _finished_stock_policy_rows(
    db: Session,
    *,
    product_id: int,
    user: User,
) -> tuple[Product, list[InventoryStockPolicy]]:
    product = db.get(Product, product_id)
    if (
        product is None
        or product.deleted_at is not None
        or not product.is_active
    ):
        raise HTTPException(status_code=404, detail="常用箱不存在或已停用。")
    require_customer_access(product.customer_id, user, db)
    rows = _active_finished_stock_policies(db, product_id=product.id)
    rows = [
        row
        for row in rows
        if _stock_policy_customer_id(db, row, relationships_loaded=True)
        == product.customer_id
    ]
    if len(rows) > 1:
        raise HTTPException(
            status_code=409,
            detail="该常用箱存在多条启用中的库存预警，请先由管理员合并后再修改。",
        )
    return product, rows


def _finished_stock_policy_quick_summary(
    db: Session,
    *,
    product: Product,
    policy: InventoryStockPolicy | None,
) -> dict:
    quantities = finished_product_quantity_summary(
        db,
        product_id=product.id,
        customer_id=product.customer_id,
    )
    warning = int(policy.warning_quantity or 0) if policy else 0
    target = int(policy.target_quantity or 0) if policy else 0
    available = quantities["available_quantity"]
    return {
        "id": policy.id if policy else None,
        "product_id": product.id,
        "customer_id": product.customer_id,
        "customer_name": product.customer.name if product.customer else None,
        "product_code": product.product_code,
        "product_name": product.product_name,
        "warning_quantity": warning,
        "target_quantity": target,
        **quantities,
        "warning_triggered": bool(policy and available < warning),
        "suggested_replenishment_quantity": (
            max(target - available, 0) if policy else 0
        ),
    }


@router.get("/stock-policies/finished-products/{product_id}")
def read_finished_stock_policy_quick(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    product, rows = _finished_stock_policy_rows(
        db,
        product_id=product_id,
        user=user,
    )
    return _finished_stock_policy_quick_summary(
        db,
        product=product,
        policy=rows[0] if rows else None,
    )


@router.put("/stock-policies/finished-products/{product_id}")
def save_finished_stock_policy_quick(
    product_id: int,
    payload: FinishedStockPolicyQuickPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if payload.target_quantity < payload.warning_quantity:
        raise HTTPException(
            status_code=400,
            detail="建议补到数量不能小于库存下限。",
        )
    with _FINISHED_STOCK_POLICY_WRITE_LOCK:
        product, rows = _finished_stock_policy_rows(
            db,
            product_id=product_id,
            user=user,
        )
        policy = rows[0] if rows else InventoryStockPolicy(
            policy_name=f"{product.product_code or product.product_name} 成品库存预警",
            target_inventory_type="finished",
            product_id=product.id,
            customer_id=product.customer_id,
            warning_quantity=payload.warning_quantity,
            target_quantity=payload.target_quantity,
            active=True,
            created_by=user.id,
        )
        policy.warning_quantity = payload.warning_quantity
        policy.target_quantity = payload.target_quantity
        policy.customer_id = product.customer_id
        policy.updated_by = user.id
        try:
            validate_stock_policy(db, policy)
            if policy.id is None:
                db.add(policy)
            db.commit()
            policy = db.scalar(
                _stock_policy_query().where(InventoryStockPolicy.id == policy.id)
            )
            assert policy is not None
            return _finished_stock_policy_quick_summary(
                db,
                product=product,
                policy=policy,
            )
        except StockReplenishmentError as error:
            db.rollback()
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
            ) from error


@router.get("/stock-replenishment/locations")
def search_stock_replenishment_locations(
    q: str | None = None,
    target_inventory_type: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    allowed: set[str] | None = None
    if target_inventory_type:
        target = target_inventory_type.strip().lower()
        allowed = {
            "finished": {"finished", "shared"},
            "semi_finished": {"semi_finished", "shared"},
        }.get(target)
        if allowed is None:
            raise HTTPException(status_code=400, detail="库存目标类型无效。")
    rows = [
        row.location
        for row in list_operational_locations(
            db,
            warehouse_types=allowed,
        )
        if row.location.source_version != "V11"
    ]
    keyword = (q or "").strip().casefold()
    if keyword:
        rows = [
            row
            for row in rows
            if keyword in str(row.location_code or "").casefold()
            or keyword in str(row.location_name or "").casefold()
        ]
    rows.sort(key=lambda row: (row.location_code, row.id))
    return {
        "items": [
            {
                "id": row.id,
                "location_code": row.location_code,
                "location_name": row.location_name,
                "warehouse_type": row.warehouse_type,
            }
            for row in rows
        ]
    }


@router.get("/stock-replenishment/products")
def search_stock_replenishment_products(
    customer_id: int | None = None,
    q: str | None = None,
    limit: int = Query(default=30, ge=1, le=2000),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = (
        select(Product)
        .options(selectinload(Product.material))
        .where(
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
    )
    allowed_customer_ids = _allowed_customer_ids(_user, db)
    if allowed_customer_ids is not None:
        query = query.where(Product.customer_id.in_(allowed_customer_ids))
    if customer_id:
        require_customer_access(customer_id, _user, db)
        query = query.where(Product.customer_id == customer_id)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
            )
        )
    rows = db.scalars(query.order_by(Product.product_code).limit(limit)).all()
    return {
        "items": [
            {
                "id": row.id,
                "customer_id": row.customer_id,
                "product_code": row.product_code,
                "customer_material_code": row.customer_material_code,
                "product_name": row.product_name,
                "length_mm": row.length_mm,
                "width_mm": row.width_mm,
                "height_mm": row.height_mm,
                "material_id": row.material_id,
                "layer_count": row.layer_count,
                "flute_type": row.flute_type,
                "report_length_mm": row.report_length_mm,
                "report_width_mm": row.report_width_mm,
                "crease_type": row.crease_type,
                "crease_left_mm": row.crease_left_mm,
                "crease_middle_mm": row.crease_middle_mm,
                "crease_right_mm": row.crease_right_mm,
                "splice_mode": row.splice_mode,
                "pieces_per_box": row.pieces_per_box,
                "material_code": (
                    row.material.code
                    if row.material is not None
                    else row.default_material_code or row.legacy_material_text
                ),
                "material_supplier_name": (
                    row.material.supplier_name if row.material is not None else None
                ),
            }
            for row in rows
        ]
    }


@router.post("/stock-policies", status_code=status.HTTP_201_CREATED)
def create_stock_policy(
    payload: StockPolicyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    guard = (
        _FINISHED_STOCK_POLICY_WRITE_LOCK
        if payload.target_inventory_type == "finished"
        else nullcontext()
    )
    with guard:
        existing_rows = (
            _active_finished_stock_policies(db, product_id=payload.product_id)
            if payload.target_inventory_type == "finished"
            and payload.product_id is not None
            and payload.active
            else []
        )
        if len(existing_rows) > 1:
            raise HTTPException(
                status_code=409,
                detail="该常用箱已存在多条启用中的库存预警，请联系管理员处理。",
            )
        row = existing_rows[0] if existing_rows else InventoryStockPolicy(
            policy_name=payload.policy_name,
            target_inventory_type=payload.target_inventory_type,
            target_quantity=payload.target_quantity,
            warning_quantity=payload.warning_quantity,
            created_by=user.id,
            updated_by=user.id,
        )
        try:
            _apply_stock_policy_payload(row, payload, user_id=user.id)
            validate_stock_policy(db, row)
            _require_stock_policy_customer_access(db, row, user)
            if row.id is None:
                db.add(row)
            db.commit()
            row = db.scalar(
                _stock_policy_query().where(InventoryStockPolicy.id == row.id)
            )
            assert row is not None
            return _stock_policy_summary(db, row, user)
        except HTTPException:
            db.rollback()
            raise
        except (StockReplenishmentError, WarehouseInventoryError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 400), detail=str(error)
            ) from error


@router.put("/stock-policies/{policy_id}")
def update_stock_policy(
    policy_id: int,
    payload: StockPolicyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    row = db.get(InventoryStockPolicy, policy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库存预警策略不存在。")
    guard = (
        _FINISHED_STOCK_POLICY_WRITE_LOCK
        if row.target_inventory_type == "finished"
        or payload.target_inventory_type == "finished"
        else nullcontext()
    )
    with guard:
        _require_stock_policy_customer_access(db, row, user)
        try:
            _apply_stock_policy_payload(row, payload, user_id=user.id)
            validate_stock_policy(db, row)
            if (
                row.active
                and row.target_inventory_type == "finished"
                and row.product_id is not None
                and _active_finished_stock_policies(
                    db,
                    product_id=row.product_id,
                    exclude_policy_id=row.id,
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="该常用箱已有启用中的库存预警，请直接修改原预警。",
                )
            _require_stock_policy_customer_access(db, row, user)
            db.commit()
            row = db.scalar(
                _stock_policy_query().where(InventoryStockPolicy.id == policy_id)
            )
            assert row is not None
            return _stock_policy_summary(db, row, user)
        except HTTPException:
            db.rollback()
            raise
        except (StockReplenishmentError, WarehouseInventoryError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 400), detail=str(error)
            ) from error


@router.get("/stock-policies/{policy_id}/replenishment-draft")
def stock_policy_replenishment_draft(
    policy_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    policy = db.scalar(_stock_policy_query().where(InventoryStockPolicy.id == policy_id))
    if policy is None:
        raise HTTPException(status_code=404, detail="库存预警策略不存在。")
    _require_stock_policy_customer_access(db, policy, _user)
    summary = _stock_policy_summary(db, policy, _user)
    product = policy.product
    if product is None and policy.target_inventory_type == "semi_finished":
        return {
            "source_type": "stock_warning",
            "supplier_name": policy.supplier_name,
            "customer_id": policy.customer_id,
            "stock_now": False,
            "draft_ready": True,
            "missing_fields": [],
            "items": [
                {
                    "stock_policy_id": policy.id,
                    "target_inventory_type": policy.target_inventory_type,
                    "product_id": None,
                    "customer_id": policy.customer_id,
                    "product_code": None,
                    "product_name": policy.policy_name,
                    "material_id": None,
                    "material_code": policy.material_code_snapshot,
                    "material_supplier_name": policy.supplier_name,
                    "layer_count": policy.layer_count,
                    "flute_type": policy.flute_type,
                    "report_length_mm": policy.report_length_mm,
                    "report_width_mm": policy.report_width_mm,
                    "crease_type": None,
                    "crease_left_mm": None,
                    "crease_middle_mm": None,
                    "crease_right_mm": None,
                    "sheet_type": policy.sheet_type,
                    "component_type": policy.component_type,
                    "pieces_per_box": policy.pieces_per_box,
                    "stock_yield_per_sheet": policy.stock_yield_per_sheet,
                    "quantity": summary["suggested_replenishment_quantity"],
                    "location_id": None,
                    "remark": policy.remark,
                    "cutting_mode": "一开一",
                    "output_per_sheet": 1,
                    "theoretical_requisition_quantity": summary[
                        "suggested_replenishment_quantity"
                    ],
                    "draft_ready": True,
                    "missing_fields": [],
                }
            ],
            "compatible_products": [],
            "policy_summary": summary,
        }
    if product is None or product.deleted_at is not None or not product.is_active:
        raise HTTPException(status_code=409, detail="库存预警关联的常用箱不可用。")
    defaults = product_replenishment_defaults(product)

    def draft_item(
        draft_policy: InventoryStockPolicy,
        draft_summary: dict,
        draft_product: Product,
    ) -> dict:
        product_defaults = product_replenishment_defaults(draft_product)
        finished_quantity = int(
            draft_summary.get(
                "suggested_new_requisition_finished_quantity",
                draft_summary["suggested_replenishment_quantity"],
            )
            or 0
        )
        crease_type = product_defaults["crease_type"]
        sheet_type = (
            "creased_sheet"
            if crease_type == "压线"
            else "net_sheet"
            if crease_type == "净料"
            else "raw_board"
        )
        theoretical_quantity = theoretical_requisition_quantity(
            finished_quantity,
            product_defaults["cutting_mode"],
        )
        return {
            "stock_policy_id": draft_policy.id,
            "target_inventory_type": "semi_finished",
            "product_id": draft_product.id,
            "customer_id": draft_product.customer_id,
            "product_code": draft_product.product_code,
            "product_name": draft_product.product_name,
            "material_id": product_defaults["material_id"],
            "material_code": (
                draft_policy.material_code_snapshot
                or product_defaults["material_code"]
            ),
            "material_supplier_name": (
                product_defaults["material_supplier_name"]
                or draft_policy.supplier_name
            ),
            "layer_count": (
                draft_policy.layer_count or product_defaults["layer_count"]
            ),
            "flute_type": (
                draft_policy.flute_type or product_defaults["flute_type"]
            ),
            "report_length_mm": (
                draft_policy.report_length_mm
                or product_defaults["report_length_mm"]
            ),
            "report_width_mm": (
                draft_policy.report_width_mm
                or product_defaults["report_width_mm"]
            ),
            "crease_type": crease_type,
            "crease_left_mm": product_defaults["crease_left_mm"],
            "crease_middle_mm": product_defaults["crease_middle_mm"],
            "crease_right_mm": product_defaults["crease_right_mm"],
            "sheet_type": sheet_type,
            "component_type": draft_policy.component_type,
            "pieces_per_box": product_defaults["pieces_per_box"],
            "stock_yield_per_sheet": product_defaults["output_per_sheet"],
            "quantity": theoretical_quantity,
            "suggested_finished_quantity": finished_quantity,
            "location_id": None,
            "remark": draft_policy.remark,
            "cutting_mode": product_defaults["cutting_mode"],
            "output_per_sheet": product_defaults["output_per_sheet"],
            "theoretical_requisition_quantity": theoretical_quantity,
            "customer_board_preparation_available_sheet_quantity": int(
                draft_summary.get(
                    "customer_board_preparation_available_sheet_quantity",
                    0,
                )
                or 0
            ),
            "incoming_board_preparation_sheet_quantity": int(
                draft_summary.get(
                    "incoming_board_preparation_sheet_quantity",
                    0,
                )
                or 0
            ),
            "draft_ready": product_defaults["draft_ready"],
            "missing_fields": product_defaults["missing_fields"],
        }

    primary_item = draft_item(policy, summary, product)
    signature = product_replenishment_signature(product)
    compatible_board_products = [
        {
            "product_id": product.id,
            "product_code": product.product_code,
            "product_name": product.product_name,
        }
    ]
    compatible_products: list[dict] = []
    if signature is not None:
        candidates = db.scalars(
            select(Product)
            .options(
                selectinload(Product.material),
                selectinload(Product.customer),
            )
            .where(
                Product.customer_id == product.customer_id,
                Product.id != product.id,
                Product.is_active.is_(True),
                Product.deleted_at.is_(None),
            )
            .order_by(Product.product_code)
        ).all()
        for candidate in candidates:
            if product_replenishment_signature(candidate) != signature:
                continue
            compatible_board_products.append(
                {
                    "product_id": candidate.id,
                    "product_code": candidate.product_code,
                    "product_name": candidate.product_name,
                }
            )
            candidate_policies = _active_finished_stock_policies(
                db,
                product_id=candidate.id,
            )
            if len(candidate_policies) != 1:
                continue
            candidate_policy = candidate_policies[0]
            if _stock_policy_customer_id(
                db,
                candidate_policy,
                relationships_loaded=True,
            ) != candidate.customer_id:
                continue
            candidate_summary = _stock_policy_summary(
                db,
                candidate_policy,
                _user,
            )
            compatible_products.append(
                {
                    "policy_id": candidate_policy.id,
                    "product_id": candidate.id,
                    "product_code": candidate.product_code,
                    "product_name": candidate.product_name,
                    "available_quantity": candidate_summary[
                        "available_quantity"
                    ],
                    "warning_quantity": candidate_summary["warning_quantity"],
                    "target_quantity": candidate_summary["target_quantity"],
                    "warning_triggered": candidate_summary[
                        "warning_triggered"
                    ],
                    "suggested_replenishment_quantity": candidate_summary[
                        "suggested_replenishment_quantity"
                    ],
                    "suggested_new_requisition_finished_quantity": (
                        candidate_summary[
                            "suggested_new_requisition_finished_quantity"
                        ]
                    ),
                    "suggested_new_requisition_sheet_quantity": (
                        candidate_summary[
                            "suggested_new_requisition_sheet_quantity"
                        ]
                    ),
                    "replenishment_state": candidate_summary[
                        "replenishment_state"
                    ],
                    "draft_item": draft_item(
                        candidate_policy,
                        candidate_summary,
                        candidate,
                    ),
                }
            )
    compatible_products.sort(
        key=lambda item: (
            not item["warning_triggered"],
            item["product_code"] or "",
        )
    )
    compatible_board_products.sort(
        key=lambda item: item["product_code"] or ""
    )
    compatible_board_product_ids = [
        item["product_id"] for item in compatible_board_products
    ]
    compatible_board_product_codes = [
        item["product_code"] for item in compatible_board_products
    ]
    for draft in [
        primary_item,
        *[
            item["draft_item"]
            for item in compatible_products
            if item.get("draft_item")
        ],
    ]:
        draft["compatible_product_ids"] = compatible_board_product_ids
        draft["compatible_product_codes"] = compatible_board_product_codes
    return {
        "source_type": "stock_warning",
        "supplier_name": (
            defaults["material_supplier_name"] or policy.supplier_name
        ),
        "customer_id": policy.customer_id,
        "stock_now": False,
        "draft_ready": primary_item["draft_ready"],
        "missing_fields": primary_item["missing_fields"],
        "items": [primary_item],
        "compatible_products": compatible_products,
        "compatible_board_products": compatible_board_products,
        "policy_summary": summary,
    }


def _replenishment_order_query():
    return select(StockReplenishmentOrder).options(
        selectinload(StockReplenishmentOrder.customer),
        selectinload(StockReplenishmentOrder.items).selectinload(
            StockReplenishmentOrderItem.location
        ),
        selectinload(StockReplenishmentOrder.items).selectinload(
            StockReplenishmentOrderItem.inventory_lot
        ).selectinload(InventoryLot.allowed_products).selectinload(
            SemiFinishedLotAllowedProduct.product
        ),
        selectinload(StockReplenishmentOrder.items).selectinload(
            StockReplenishmentOrderItem.customer
        ),
        selectinload(StockReplenishmentOrder.items).selectinload(
            StockReplenishmentOrderItem.product
        ),
        selectinload(StockReplenishmentOrder.items)
        .selectinload(StockReplenishmentOrderItem.stock_policy)
        .selectinload(InventoryStockPolicy.customer),
        selectinload(StockReplenishmentOrder.items)
        .selectinload(StockReplenishmentOrderItem.stock_policy)
        .selectinload(InventoryStockPolicy.product),
    )


def _coalesce(value, fallback):
    return fallback if value in (None, "") else value


def _build_replenishment_item(
    db: Session,
    payload: StockReplenishmentItemPayload,
    *,
    source_type: str,
) -> StockReplenishmentOrderItem:
    policy = db.get(InventoryStockPolicy, payload.stock_policy_id) if payload.stock_policy_id else None
    if payload.stock_policy_id and policy is None:
        raise StockReplenishmentError("库存预警策略不存在。", 404)
    if policy and policy.target_inventory_type != payload.target_inventory_type:
        warning_finished_to_customer_board = (
            source_type == "stock_warning"
            and policy.target_inventory_type == "finished"
            and payload.target_inventory_type == "semi_finished"
            and policy.product_id is not None
        )
        if not warning_finished_to_customer_board:
            raise StockReplenishmentError("补库明细类型与库存预警策略不一致。")

    product_id = _coalesce(payload.product_id, policy.product_id if policy else None)
    product = db.get(Product, product_id) if product_id else None
    if product_id and (product is None or product.deleted_at is not None):
        raise StockReplenishmentError("补库明细产品不存在。", 404)
    customer_id = _coalesce(
        payload.customer_id,
        policy.customer_id if policy else (product.customer_id if product else None),
    )
    material = db.get(Material, payload.material_id) if payload.material_id else None
    if payload.material_id and (material is None or not material.is_active):
        raise StockReplenishmentError("补库明细材质主数据不存在或已停用。", 404)
    material_code = _coalesce(
        material.code if material else payload.material_code,
        policy.material_code_snapshot if policy else None,
    )
    normalized_material = normalize_material_code(material_code) if material_code else None
    layer_count = _coalesce(
        material.layer_count if material else payload.layer_count,
        policy.layer_count if policy else None,
    )
    flute_type = _coalesce(payload.flute_type, policy.flute_type if policy else None)
    flute_type, flute_error = _business_flute_error(layer_count, flute_type)
    report_length = _coalesce(
        payload.report_length_mm,
        policy.report_length_mm if policy else None,
    )
    report_width = _coalesce(
        payload.report_width_mm,
        policy.report_width_mm if policy else None,
    )
    location_id = (
        None
        if payload.target_inventory_type == "semi_finished"
        else _coalesce(
            payload.location_id,
            policy.default_location_id if policy else None,
        )
    )
    if location_id:
        location = db.get(WarehouseLocation, location_id)
        if location is None or not location.is_active:
            raise StockReplenishmentError("补库明细库位不存在或已停用。")
        if location.source_version == "V11":
            raise StockReplenishmentError(
                "V11 三楼 Phase A 货位不能用于正式库存补库。", 409
            )
        allowed = {
            "finished": {"finished", "shared"},
            "semi_finished": {"semi_finished", "shared"},
        }[payload.target_inventory_type]
        if location.warehouse_type not in allowed:
            raise StockReplenishmentError("补库明细库位类型不匹配。")
        location_issue = operational_location_issue(
            db,
            location,
            warehouse_types=allowed,
        )
        if location_issue:
            raise StockReplenishmentError(
                f"补库明细库位不可使用：{location_issue}", 409
            )

    if payload.target_inventory_type == "finished" and product is None:
        raise StockReplenishmentError("成品补库必须选择产品。")
    if payload.target_inventory_type == "semi_finished":
        required = (material_code, layer_count, flute_type, report_length, report_width)
        if not all(value not in (None, "") for value in required):
            raise StockReplenishmentError(
                "半成品补库必须填写材质、层数、楞型和报料长宽。"
            )
        if layer_count not in {3, 5, 7}:
            raise StockReplenishmentError("半成品补库层数只允许三层、五层或七层。")
        code_error = seven_layer_code_error(material_code, layer_count)
        if code_error:
            raise StockReplenishmentError(code_error)
        if flute_error:
            raise StockReplenishmentError(flute_error)
        crease_error = crease_width_error(
            label="压线",
            crease_type=payload.crease_type,
            report_width_mm=report_width,
            left_mm=payload.crease_left_mm,
            middle_mm=payload.crease_middle_mm,
            right_mm=payload.crease_right_mm,
        )
        if crease_error:
            raise StockReplenishmentError(f"{crease_error}。")

    return StockReplenishmentOrderItem(
        stock_policy_id=policy.id if policy else None,
        target_inventory_type=payload.target_inventory_type,
        product_id=product.id if product else None,
        customer_id=customer_id,
        material_id=material.id if material else None,
        product_code_snapshot=(payload.product_code or (product.product_code if product else None)),
        product_name_snapshot=(payload.product_name or (product.product_name if product else None) or (policy.policy_name if policy else "库存片料")),
        material_code_snapshot=material_code,
        normalized_material_code=normalized_material,
        layer_count=layer_count,
        flute_type=flute_type,
        report_length_mm=report_length,
        report_width_mm=report_width,
        crease_type=payload.crease_type,
        crease_left_mm=payload.crease_left_mm,
        crease_middle_mm=payload.crease_middle_mm,
        crease_right_mm=payload.crease_right_mm,
        sheet_type=payload.sheet_type or (policy.sheet_type if policy else "raw_board"),
        component_type=payload.component_type or (policy.component_type if policy else "whole"),
        pieces_per_box=payload.pieces_per_box or (policy.pieces_per_box if policy else 1),
        stock_yield_per_sheet=payload.stock_yield_per_sheet or (policy.stock_yield_per_sheet if policy else 1),
        quantity=payload.quantity,
        location_id=location_id,
        historical_workbook=payload.historical_workbook,
        historical_sheet=payload.historical_sheet,
        historical_row=payload.historical_row,
        historical_search_text=payload.historical_search_text,
        remark=payload.remark,
    )


@router.post("/stock-replenishment/orders", status_code=status.HTTP_201_CREATED)
def create_stock_replenishment_order(
    payload: StockReplenishmentCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    idempotent_order_number: str | None = None
    try:
        if payload.source_type == "manual_history":
            raise StockReplenishmentError(
                "旧“历史采购检索”新建入口已停用；"
                "请使用库存预警或手动选择客户和常用箱生成报料草稿。",
                409,
            )
        if payload.stock_now:
            raise StockReplenishmentError(
                "库存补库只能先生成报料草稿，不能保存后直接写入库存。"
            )
        if any(
            item.target_inventory_type != "semi_finished"
            for item in payload.items
        ):
            raise StockReplenishmentError(
                "新建库存补库到料只能进入客户专用纸板备料，"
                "不能直接生成成品库存。"
            )
        if payload.source_type == "stock_warning" and payload.idempotency_key is None:
            raise StockReplenishmentError(
                "库存预警报料草稿缺少防重复标识，请关闭后重新打开再保存。"
            )
        if payload.idempotency_key:
            key_digest = hashlib.sha256(
                payload.idempotency_key.encode("utf-8")
            ).hexdigest()[:20].upper()
            prefix = (
                "CBR" if payload.source_type == "customer_request" else "CBW"
            )
            idempotent_order_number = (
                f"{prefix}-{beijing_today():%Y%m%d}-{key_digest}"
            )
            existing_order = db.scalar(
                _replenishment_order_query().where(
                    StockReplenishmentOrder.order_number
                    == idempotent_order_number
                )
            )
            if existing_order is not None:
                _require_stock_replenishment_order_access(
                    db,
                    existing_order,
                    user,
                    relationships_loaded=True,
                )
                return replenishment_order_dict(existing_order)
        items = [
            _build_replenishment_item(
                db,
                item,
                source_type=payload.source_type,
            )
            for item in payload.items
        ]
        if payload.source_type == "stock_warning":
            for item in items:
                if (
                    item.product_id is not None
                    and item.target_inventory_type != "semi_finished"
                ):
                    raise StockReplenishmentError(
                        "常用箱库存预警报料只能生成客户专用纸板备料，"
                        "不能直接生成成品库存。"
                    )
                if not all(
                    value not in (None, "")
                    for value in (
                        item.product_id,
                        item.material_code_snapshot,
                        item.layer_count,
                        item.flute_type,
                        item.report_length_mm,
                        item.report_width_mm,
                    )
                ):
                    raise StockReplenishmentError(
                        f"“{item.product_name_snapshot}”的常用箱资料不完整，"
                        "请先补全材质、层数、楞型和报料长宽。"
                    )
                if item.product_id is not None:
                    product = db.scalar(
                        select(Product)
                        .options(selectinload(Product.material))
                        .where(Product.id == item.product_id)
                    )
                    if product is None:
                        raise StockReplenishmentError(
                            "库存预警关联的常用箱不存在。"
                        )
                    defaults = product_replenishment_defaults(product)
                    if not defaults["draft_ready"]:
                        raise StockReplenishmentError(
                            f"“{item.product_name_snapshot}”的常用箱资料不完整，"
                            "请先补全后重新生成草稿。"
                        )
                    expected = {
                        "customer_id": product.customer_id,
                        "material_id": defaults["material_id"],
                        "material_code": normalize_material_code(
                            defaults["material_code"]
                        ),
                        "layer_count": defaults["layer_count"],
                        "flute_type": defaults["flute_type"],
                        "report_length_mm": defaults["report_length_mm"],
                        "report_width_mm": defaults["report_width_mm"],
                        "crease_type": defaults["crease_type"],
                        "crease_left_mm": defaults["crease_left_mm"],
                        "crease_middle_mm": defaults["crease_middle_mm"],
                        "crease_right_mm": defaults["crease_right_mm"],
                        "sheet_type": (
                            "creased_sheet"
                            if defaults["crease_type"] == "压线"
                            else "net_sheet"
                            if defaults["crease_type"] == "净料"
                            else "raw_board"
                        ),
                        "pieces_per_box": defaults["pieces_per_box"],
                        "stock_yield_per_sheet": defaults["output_per_sheet"],
                    }
                    actual = {
                        "customer_id": item.customer_id,
                        "material_id": item.material_id,
                        "material_code": item.normalized_material_code,
                        "layer_count": item.layer_count,
                        "flute_type": item.flute_type,
                        "report_length_mm": item.report_length_mm,
                        "report_width_mm": item.report_width_mm,
                        "crease_type": item.crease_type,
                        "crease_left_mm": item.crease_left_mm,
                        "crease_middle_mm": item.crease_middle_mm,
                        "crease_right_mm": item.crease_right_mm,
                        "sheet_type": item.sheet_type,
                        "pieces_per_box": item.pieces_per_box,
                        "stock_yield_per_sheet": item.stock_yield_per_sheet,
                    }
                    if actual != expected:
                        raise StockReplenishmentError(
                            f"“{item.product_name_snapshot}”的纸板备料参数"
                            "必须与常用箱主数据一致，请刷新后重新生成草稿。"
                        )
        material_suppliers = {
            material.supplier_name.strip()
            for item in items
            if item.material_id
            and (material := db.get(Material, item.material_id)) is not None
            and material.supplier_name
            and material.supplier_name.strip()
        }
        if len(material_suppliers) > 1:
            raise StockReplenishmentError(
                "一张库存补库单只能包含同一供应商的材质，请分开保存。"
            )
        derived_supplier = next(iter(material_suppliers), None)
        for supplier_name in material_suppliers:
            _require_active_supplier(db, supplier_name)
        payload_supplier = (payload.supplier_name or "").strip()
        if payload_supplier:
            payload_supplier = _require_active_supplier(db, payload_supplier)
        order = StockReplenishmentOrder(
            order_number=(
                idempotent_order_number
                or next_replenishment_order_number()
            ),
            supplier_name=derived_supplier
            or payload_supplier
            or None,
            customer_id=payload.customer_id,
            source_type=payload.source_type,
            status="confirmed",
            remark=payload.remark,
            created_by=user.id,
            confirmed_by=user.id,
            confirmed_at=utc_now_naive(),
        )
        order.items = items
        _require_stock_replenishment_order_access(db, order, user)
        db.add(order)
        db.flush()
        if payload.stock_now:
            stock_replenishment_order(db, order=order, operator_id=user.id)
        db.commit()
        order = db.scalar(
            _replenishment_order_query().where(StockReplenishmentOrder.id == order.id)
        )
        assert order is not None
        return replenishment_order_dict(order)
    except IntegrityError:
        db.rollback()
        if idempotent_order_number is not None:
            existing_order = db.scalar(
                _replenishment_order_query().where(
                    StockReplenishmentOrder.order_number
                    == idempotent_order_number
                )
            )
            if existing_order is not None:
                _require_stock_replenishment_order_access(
                    db,
                    existing_order,
                    user,
                    relationships_loaded=True,
                )
                return replenishment_order_dict(existing_order)
        raise
    except HTTPException:
        db.rollback()
        raise
    except (StockReplenishmentError, WarehouseInventoryError) as error:
        db.rollback()
        raise HTTPException(
            status_code=getattr(error, "status_code", 400), detail=str(error)
        ) from error


@router.get("/stock-replenishment/orders")
def list_stock_replenishment_orders(
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = _replenishment_order_query()
    if status_filter:
        query = query.where(StockReplenishmentOrder.status == status_filter.strip())
    rows = db.scalars(query.order_by(StockReplenishmentOrder.id.desc())).all()
    rows = [
        row
        for row in rows
        if _stock_replenishment_order_is_visible(
            db, row, _user, relationships_loaded=True
        )
    ]
    return {"items": [replenishment_order_dict(row) for row in rows]}


@router.get("/stock-replenishment/orders/{order_id}")
def get_stock_replenishment_order(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    order = db.scalar(
        _replenishment_order_query().where(StockReplenishmentOrder.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="库存补库单不存在。")
    _require_stock_replenishment_order_access(
        db, order, _user, relationships_loaded=True
    )
    return replenishment_order_dict(order)


@router.get("/stock-replenishment/orders/{order_id}/print")
def print_stock_replenishment_order(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    order = db.scalar(
        _replenishment_order_query().where(StockReplenishmentOrder.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="库存补库单不存在。")
    _require_stock_replenishment_order_access(
        db, order, _user, relationships_loaded=True
    )
    payload = replenishment_order_dict(order)
    payload["sender"] = _company_sender(db)
    return payload


@router.post("/stock-replenishment/orders/{order_id}/stock")
def stock_saved_replenishment_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    order = db.scalar(
        _replenishment_order_query().where(StockReplenishmentOrder.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="库存补库单不存在。")
    _require_stock_replenishment_order_access(
        db, order, user, relationships_loaded=True
    )
    if (
        order.status in {"confirmed", "partially_stocked"}
        and order.source_type != "manual_history"
    ):
        raise HTTPException(
            status_code=409,
            detail="补库报料必须到“仓库 → 来料入库 → 待入库”确认实际收货，"
            "不能从已报料页面直接增加库存。",
        )
    try:
        stock_replenishment_order(db, order=order, operator_id=user.id)
        db.commit()
        order = db.scalar(
            _replenishment_order_query().where(StockReplenishmentOrder.id == order_id)
        )
        assert order is not None
        return replenishment_order_dict(order)
    except (StockReplenishmentError, WarehouseInventoryError) as error:
        db.rollback()
        raise HTTPException(
            status_code=getattr(error, "status_code", 400), detail=str(error)
        ) from error


@router.put("/stock-replenishment/orders/{order_id}/void")
def void_stock_replenishment_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    order = db.scalar(
        _replenishment_order_query().where(StockReplenishmentOrder.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="库存补库单不存在。")
    _require_stock_replenishment_order_access(
        db, order, user, relationships_loaded=True
    )
    if order.status == "voided":
        return replenishment_order_dict(order)
    received_quantity = sum(int(item.stocked_quantity or 0) for item in order.items)
    receipt_fact_count = int(
        db.scalar(
            select(func.count(IncomingReceiptItem.id))
            .join(
                StockReplenishmentOrderItem,
                StockReplenishmentOrderItem.id
                == IncomingReceiptItem.stock_replenishment_item_id,
            )
            .where(
                StockReplenishmentOrderItem.replenishment_order_id == order.id,
                IncomingReceiptItem.status == "posted",
            )
        )
        or 0
    )
    if (
        order.status != "confirmed"
        or received_quantity > 0
        or receipt_fact_count > 0
    ):
        raise HTTPException(
            status_code=409,
            detail="该补库单已经部分或全部实际收货，不能直接撤销报料。",
        )
    order_number = order.order_number
    voided_at = utc_now_naive()
    transition = db.execute(
        update(StockReplenishmentOrder)
        .where(
            StockReplenishmentOrder.id == order_id,
            StockReplenishmentOrder.status == "confirmed",
        )
        .values(status="voided", voided_at=voided_at)
        .execution_options(synchronize_session=False)
    )
    if transition.rowcount != 1:
        db.rollback()
        current = db.scalar(
            _replenishment_order_query()
            .where(StockReplenishmentOrder.id == order_id)
            .execution_options(populate_existing=True)
        )
        if current is None:
            raise HTTPException(status_code=404, detail="库存补库单不存在。")
        _require_stock_replenishment_order_access(
            db, current, user, relationships_loaded=True
        )
        if current.status == "voided":
            return replenishment_order_dict(current)
        raise HTTPException(
            status_code=409,
            detail="该补库单状态已变化，可能已经收货，不能直接撤销报料。",
        )
    db.add(
        OperationLog(
            user_id=user.id,
            action="VOID_STOCK_REPLENISHMENT",
            resource="StockReplenishmentOrder",
            details=json.dumps(
                {
                    "stock_replenishment_order_id": order.id,
                    "order_number": order_number,
                    "received_quantity": 0,
                    "inventory_created": False,
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="stock_replenishment_order",
            entity_id=order.id,
            description="补库报料在实际收货前撤销",
        )
    )
    db.commit()
    order = db.scalar(
        _replenishment_order_query()
        .where(StockReplenishmentOrder.id == order_id)
        .execution_options(populate_existing=True)
    )
    assert order is not None
    return replenishment_order_dict(order)


@router.get("/historical-purchases/search")
def search_historical_purchase_source(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    user = _user
    allowed = _allowed_customer_ids(user, db)
    if allowed is None:
        return search_historical_purchase_database(db, q, limit=limit)

    # Customer attribution is optional in imported history.  Scoped accounts
    # intentionally see only attributed rows, with the scope applied before
    # grouping and limiting so unrelated history cannot crowd out valid hits.
    normalized_query = normalize_lookup_text(q)
    indexed_records = int(
        db.scalar(
            select(func.count(HistoricalPurchaseEntry.id)).where(
                HistoricalPurchaseEntry.customer_id.in_(allowed)
            )
        )
        or 0
    )
    if not normalized_query:
        return {
            "query": q,
            "items": [],
            "total_matches": 0,
            "source_record_matches": 0,
            "indexed_records": indexed_records,
            "source_workbook": None,
            "source_sheet": DEFAULT_SHEET_NAME,
        }
    matching_rows = list(
        db.scalars(
            select(HistoricalPurchaseEntry)
            .where(
                HistoricalPurchaseEntry.customer_id.in_(allowed),
                HistoricalPurchaseEntry.normalized_search_text.contains(
                    normalized_query
                ),
            )
            .order_by(
                HistoricalPurchaseEntry.record_date.desc(),
                HistoricalPurchaseEntry.source_row.desc(),
            )
        )
    )
    grouped_rows: dict[tuple, list[HistoricalPurchaseEntry]] = {}
    for row in matching_rows:
        grouped_rows.setdefault(
            _historical_purchase_display_key(row), []
        ).append(row)
    groups = list(grouped_rows.values())
    return {
        "query": q,
        "items": [
            _historical_purchase_group_response(group)
            for group in groups[:limit]
        ],
        "total_matches": len(groups),
        "source_record_matches": len(matching_rows),
        "indexed_records": indexed_records,
        "source_workbook": (
            matching_rows[0].source_workbook if matching_rows else None
        ),
        "source_sheet": (
            matching_rows[0].source_sheet
            if matching_rows
            else DEFAULT_SHEET_NAME
        ),
    }


@router.get("/search_history")
def search_history(
    keyword: str = Query(min_length=1),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    user = _user
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
    allowed = _allowed_customer_ids(user, db)
    if allowed is not None:
        query = query.where(Order.customer_id.in_(allowed))
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
    user = _user
    registry = build_display_registry(db)
    query = (
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
    )
    allowed = _allowed_customer_ids(user, db)
    if allowed is not None:
        query = query.where(Order.customer_id.in_(allowed))
    rows = db.execute(query).all()

    reservation_map = requisition_finished_inventory_coverage_by_item_ids(
        db, [item.id for item, *_ in rows]
    )
    read_context = _PendingRequisitionReadContext(db, rows)

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
        material = read_context.material_for(item)
        requirements = (
            _ordinary_requisition_requirements(
                item,
                cutting_mode=DEFAULT_CUTTING_MODE,
            )
            if read_context.is_ordinary(item)
            else read_context.current_requisition_summary(
                item,
                product=product,
                cutting_mode=DEFAULT_CUTTING_MODE,
                finished_reserved_qty=reservation_map.get(item.id, 0),
            )
        )
        if not _requires_supplier_purchase(requirements):
            continue
        pieces_per_box = int(requirements["pieces_per_box"])
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        if production_required_qty == 0:
            continue
        required_piece_qty = int(requirements["required_piece_qty"])
        semi_finished_reserved_piece_qty = int(
            requirements["semi_finished_reserved_piece_qty"]
        )
        remaining_required_piece_qty = int(
            requirements["remaining_required_piece_qty"]
        )
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
            "semi_finished_reserved_piece_qty": semi_finished_reserved_piece_qty,
            "remaining_required_piece_qty": remaining_required_piece_qty,
            "requisition_qty": int(requirements["requisition_qty"]),
            "delivery_date": order.delivery_date,
        })

    suggestions = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        supplier_name, material_id, layer_count, flute_type, report_len, report_width, pieces_per_box, splice_mode, crease_type, crease_left, crease_middle, crease_right = key
        material = read_context.material_by_id.get(material_id)
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
    for item, _order, _customer, _product in rows:
        _require_order_item_customer_access(db, item, user)
    reservation_map = requisition_finished_inventory_coverage_by_item_ids(
        db, [item.id for item, *_ in rows]
    )
    supplier_name = (payload.supplier_name or "").strip()
    if supplier_name:
        supplier_name = _require_active_supplier(db, supplier_name)
    requisition_date = beijing_today()
    try:
        group = Requisition(
            requisition_number=_next_number(db, requisition_date),
            requisition_date=requisition_date,
            supplier_name=supplier_name or None,
            status="merged_pending",
            created_by=user.id,
        )
        db.add(group)
        db.flush()
        for item, _order, _customer, product in rows:
            requirements = _current_requisition_summary(
                db,
                item,
                cutting_mode=payload.cutting_mode,
                finished_reserved_qty=reservation_map.get(item.id, 0),
            )
            pieces_per_box = int(requirements["pieces_per_box"])
            production_required_qty = int(requirements["production_required_qty"])
            if production_required_qty == 0:
                raise HTTPException(
                    status_code=409,
                    detail="所选明细已由成品库存全额抵扣，不能创建待报料合并组",
                )
            required_piece_qty = int(requirements["required_piece_qty"])
            db.add(
                RequisitionItem(
                    requisition_id=group.id,
                    order_item_id=item.id,
                    inventory_deducted_qty=0,
                    requisition_qty=int(requirements["requisition_qty"]),
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
    _require_requisition_customer_access(group, user, db)
    if group.status != "merged_pending":
        raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能修改")
    try:
        if payload.supplier_name is not None:
            requested_supplier = payload.supplier_name.strip()
            current_supplier = (group.supplier_name or "").strip()
            if normalize_supplier_identity(
                requested_supplier
            ) != normalize_supplier_identity(current_supplier):
                group.supplier_name = (
                    _require_active_supplier(db, requested_supplier)
                    if requested_supplier
                    else None
                )
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
                component_type=_requisition_item_component(item),
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
    _require_requisition_customer_access(group, user, db)
    if group.status != "merged_pending":
        raise HTTPException(status_code=409, detail="该合并组已生成供应商报料单，不能重复生成")
    supplier_name = (group.supplier_name or "").strip()
    if not supplier_name:
        raise HTTPException(status_code=400, detail="请先为合并组选择供应商")
    supplier_name = _require_active_supplier(db, supplier_name)
    rows = _merge_group_rows(db, group.id)
    if not rows:
        raise HTTPException(status_code=400, detail="合并组没有来源明细")
    current_rows: list[tuple] = []
    for req_item, order_item, order_row, customer, product in rows:
        if req_item.status != "merged_pending":
            raise HTTPException(status_code=409, detail="合并组状态异常，不能生成供应商报料单")
        if order_item.requisition_status != "未报料":
            raise HTTPException(status_code=409, detail="合并组中存在已报料明细，不能重复生成")
        _ensure_order_item_crease_width(order_item)
        late_finished_inventory = _late_finished_inventory_preview(
            db,
            item=order_item,
            order=order_row,
            product=product,
        )
        if late_finished_inventory["can_auto_reserve"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "合并组中发现后来入库的同客户同存货编码成品库存，"
                    "请先返回待报料列表点击“使用成品，剩余再报”后重试"
                ),
            )
        requirements = _current_requisition_requirements(
            db,
            order_item,
            cutting_mode=req_item.special_process,
            pieces_per_box=req_item.pieces_per_box or _pieces_per_box(order_item),
            component_type=_requisition_item_component(req_item),
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
    _require_active_material_supplier(db, material)
    layer_count = material.layer_count if material else first_order_item.layer_count
    flute_type, flute_error = _business_flute_error(
        layer_count,
        first_order_item.flute_type,
    )
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)
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
        layer_count=layer_count,
        flute_type=flute_type,
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
        requisition_date = beijing_today()
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
                    **_supplier_item_snapshot_values(
                        db,
                        order_item,
                        fallback_material_id=order.material_id,
                        fallback_supplier_name=order.supplier_name,
                        fallback_layer_count=order.layer_count,
                        fallback_flute_type=order.flute_type,
                    ),
                    order_number=order_item.item_order_number,
                    product_code=req_item.product_code_snapshot,
                    product_name=req_item.product_name_snapshot,
                    source_key=_supplier_requisition_source_key(
                        order_item,
                        req_item,
                        component_type=_requisition_item_component(req_item),
                    ),
                    report_length_mm=int(req_item.cardboard_len),
                    report_width_mm=int(req_item.cardboard_width),
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
    _require_requisition_customer_access(batch, _user, db)
    if batch.status in {"merged_pending", "supplier_requisition_created"}:
        raise HTTPException(status_code=409, detail="待报料合并组不是正式报料单，不能打印")
    rows = db.execute(
        select(
            RequisitionItem,
            OrderItem,
            Material,
            RequisitionItemBomSource,
            SalesOrderItemBomComponent,
        )
        .outerjoin(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .outerjoin(Material, Material.id == OrderItem.material_id)
        .outerjoin(
            RequisitionItemBomSource,
            RequisitionItemBomSource.requisition_item_id == RequisitionItem.id,
        )
        .outerjoin(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == RequisitionItemBomSource.sales_order_item_bom_component_id,
        )
        .where(
            RequisitionItem.requisition_id == batch.id,
            RequisitionItem.status == "有效",
        )
        .order_by(RequisitionItem.id)
    ).all()
    print_items = []
    for row, order_item, material, bom_source, bom_snapshot in rows:
        order_layer_count = (
            bom_snapshot.snapshot_component_layer_count
            if bom_snapshot is not None
            else order_item.layer_count if order_item else None
        )
        order_flute_type = (
            bom_snapshot.snapshot_component_flute_type
            if bom_snapshot is not None
            else order_item.flute_type if order_item else None
        )
        material_code = (
            bom_snapshot.snapshot_component_material
            if bom_snapshot is not None
            else material.code if material else None
        )
        material_layer_count = material.layer_count if material else None
        material_flute_type = None
        component = (
            str(bom_source.component_type or "whole").strip().lower()
            if bom_source is not None
            else (
                "base"
                if row.product_name_snapshot and row.product_name_snapshot.endswith("-底")
                else "cover"
            )
        )
        if bom_snapshot is not None:
            crease_type, crease_left, crease_middle, crease_right = (
                _bom_snapshot_crease(bom_snapshot, component)
            )
        elif order_item:
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
        if row.remark:
            remarks.append(row.remark)
        component_report_notes = None
        if bom_snapshot is not None:
            component_report_notes = _bom_snapshot_report_notes(
                bom_snapshot,
                component,
            )
        elif order_item:
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
                    bom_snapshot.snapshot_component_production_process
                    if bom_snapshot is not None
                    else order_item.snapshot_production_notes if order_item else None
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
    today_str = beijing_today().strftime("%Y%m%d")
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
            "component_type": _supplier_order_item_component_type(item),
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
        if item.status == "active"
    ]


def _supplier_order_item_component_type(
    item: SupplierRequisitionOrderItem,
) -> str:
    source_key = str(item.source_key or "").strip().lower()
    if source_key.endswith(":cover"):
        return "cover"
    if source_key.endswith(":base"):
        return "base"
    product_name = str(item.product_name or "").strip()
    if product_name.endswith("-盖"):
        return "cover"
    if product_name.endswith("-底"):
        return "base"
    return "whole"


def _supplier_order_purchase_lines(
    order: SupplierRequisitionOrder,
    db: Session,
) -> list[dict]:
    line_map: dict[str, dict] = {}
    for item in order.items:
        if item.status != "active":
            continue
        order_item = db.get(OrderItem, item.order_item_id) if item.order_item_id else None
        component_type = _supplier_order_item_component_type(item)
        material_id = item.material_id or (
            order_item.material_id if order_item and order_item.material_id else order.material_id
        )
        material = db.get(Material, material_id) if material_id else None
        layer_count = (
            item.layer_count_snapshot
            or (
                order_item.layer_count
                if order_item is not None and order_item.layer_count
                else order.layer_count or (material.layer_count if material else None)
            )
        )
        flute_type = _clean_supplier_flute(
            item.flute_type_snapshot
            or (
                order_item.flute_type
                if order_item is not None and order_item.flute_type
                else order.flute_type
            )
        )
        material_code = (
            item.material_code_snapshot
            or (material.code if material else None)
            or (order_item.snapshot_material if order_item else None)
        )
        report_length = _first_int_value(
            item.report_length_mm,
            order_item.cardboard_len if order_item is not None else None,
            order_item.snapshot_report_length_mm if order_item is not None else None,
            order.report_length_mm,
        )
        report_width = _first_int_value(
            item.report_width_mm,
            order_item.cardboard_width if order_item is not None else None,
            order_item.snapshot_report_width_mm if order_item is not None else None,
            order.report_width_mm,
        )
        if order_item is not None:
            crease_type, crease_left, crease_middle, crease_right = _component_crease(
                order_item,
                component_type,
            )
        else:
            crease_type = order.crease_type
            crease_left = order.crease_left_mm
            crease_middle = order.crease_middle_mm
            crease_right = order.crease_right_mm
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
            "component_type": component_type,
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
                "component_type": component_type,
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
        "created_at": utc_naive_to_api(order.created_at) if order.created_at else None,
        "voided_at": (
            beijing_naive_to_api(order.voided_at) if order.voided_at else None
        ),
        "lines": purchase_lines,
        "items": purchase_lines,
        "source_items": source_items,
    }


@router.post("/semi-inventory/reserve-from-pending")
def reserve_semi_inventory_from_pending(
    payload: PendingSemiInventoryReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        item, _order, _customer, product = _ensure_pending_order_item_for_supplier_order(
            db, payload.order_item_id
        )
        _require_order_item_customer_access(db, item, user)
        specs = _semi_component_specs_for_requisition(
            item,
            product,
            component_type=payload.component_type,
        )
        spec = specs[0]
        length = spec.get("board_length_mm")
        width = spec.get("board_width_mm")
        material_code = (item.snapshot_material or "").strip()
        flute_type = (item.flute_type or "").strip().upper()
        if not (length and width and material_code and flute_type):
            raise HTTPException(
                status_code=409,
                detail="订单明细缺少半成品长宽、材质或楞型，不能确认库存抵扣",
            )

        lot_ids = [row.lot_id for row in payload.lots]
        if len(set(lot_ids)) != len(lot_ids):
            raise HTTPException(status_code=400, detail="同一半成品批次不能重复选择")
        inventory_lots = db.scalars(
            select(InventoryLot).where(InventoryLot.id.in_(lot_ids))
        ).all()
        if len(inventory_lots) != len(lot_ids):
            raise HTTPException(status_code=404, detail="所选半成品库存批次不存在")
        allowed = _allowed_customer_ids(user, db)
        if allowed is not None and any(
            lot.semi_finished_detail is None
            or lot.semi_finished_detail.owner_customer_id not in allowed
            for lot in inventory_lots
        ):
            raise HTTPException(status_code=403, detail="无客户库存访问权限")
        inventory_lots.sort(key=inventory_fifo_sort_key)
        first_detail = inventory_lots[0].semi_finished_detail
        if first_detail is None:
            raise HTTPException(status_code=409, detail="所选批次不是半成品库存")

        existing = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == item.id,
                OrderItemSemiRequirement.component_type == payload.component_type,
            )
        )
        stock_yield = (
            int(existing.stock_yield_per_sheet or 1)
            if existing is not None
            else int(first_detail.stock_yield_per_sheet or 1)
        )
        current = _current_requisition_requirements(
            db,
            item,
            pieces_per_box=int(spec["pieces_per_box"]),
            component_type=payload.component_type,
        )
        remaining = int(current["remaining_required_piece_qty"])
        if remaining <= 0:
            raise HTTPException(
                status_code=409,
                detail="该订单明细已由半成品库存全额抵扣，请刷新待报料列表。",
            )
        requirement = save_order_item_semi_requirement(
            db,
            order_item_id=item.id,
            component_type=payload.component_type,
            board_length_mm=int(length),
            board_width_mm=int(width),
            material_code=material_code,
            flute_type=flute_type,
            pieces_per_box=int(spec["pieces_per_box"]),
            stock_yield_per_sheet=stock_yield,
            required_piece_quantity=int(current["required_piece_qty"]),
            operator_id=user.id,
        )
        expected = requirement_signature(requirement)
        for lot in inventory_lots:
            ensure_semi_finished_lot_eligibility(
                db,
                lot=lot,
                product_id=product.id,
                customer_id=requirement.customer_id,
                expected=expected,
            )
        requested = min(payload.requested_requirement_quantity, remaining)
        result = reserve_semi_finished_inventory(
            db,
            requirement_id=requirement.id,
            requested_requirement_quantity=requested,
            lots=[
                SemiFinishedLotVersion(
                    lot_id=row.lot_id,
                    expected_version=row.expected_version,
                )
                for row in payload.lots
            ],
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            confirmed=True,
            override=payload.override,
            warning_acknowledged_codes=payload.warning_acknowledged_codes,
        )
        updated = _current_requisition_requirements(
            db,
            item,
            pieces_per_box=int(spec["pieces_per_box"]),
            component_type=payload.component_type,
        )
        _audit(
            db,
            user=user,
            action="RESERVE_LATE_SEMI_INVENTORY_FROM_REQUISITION",
            entity_id=item.id,
            details={
                "order_item_id": item.id,
                "component_type": payload.component_type,
                "inventory_lot_ids": lot_ids,
                "allocated_requirement_quantity": (
                    result.allocated_requirement_quantity
                ),
                "override": payload.override,
            },
            description="合并报料前重新检查并确认半成品库存抵扣",
        )
        db.commit()
        return {
            "order_item_id": item.id,
            "requirement_id": requirement.id,
            "component_type": payload.component_type,
            "requested_requirement_quantity": (
                result.requested_requirement_quantity
            ),
            "allocated_requirement_quantity": (
                result.allocated_requirement_quantity
            ),
            "unallocated_requirement_quantity": (
                result.unallocated_requirement_quantity
            ),
            "remaining_requirement_quantity": int(
                updated["remaining_required_piece_qty"]
            ),
            "requisition_qty": int(updated["requisition_qty"]),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except HTTPException:
        db.rollback()
        raise


def _late_finished_idempotent_replay(
    db: Session,
    *,
    item_id: int,
    operation_prefix: str,
    user: User,
) -> dict | None:
    repeated = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.idempotency_key.like(
                f"{operation_prefix}%"
            )
        )
        .order_by(InventoryReservation.id)
    ).all()
    if not repeated:
        return None
    if any(
        row.order_item_id != item_id
        or row.reservation_type != "finished_order"
        for row in repeated
    ):
        raise HTTPException(
            status_code=409,
            detail="该请求标识已用于其他库存预占",
        )
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    _require_order_item_customer_access(db, item, user)
    order = db.get(Order, item.order_id)
    product = db.get(Product, item.product_id)
    if order is None or product is None:
        raise HTTPException(status_code=409, detail="订单或产品快照关联已失效")
    updated = _current_requisition_summary(db, item)
    current_preview = _late_finished_inventory_preview(
        db,
        item=item,
        order=order,
        product=product,
    )
    return {
        "order_item_id": item.id,
        "allocated_quantity": sum(
            int(row.credited_requirement_quantity or 0)
            for row in repeated
        ),
        "finished_inventory_reserved_qty": int(
            updated["finished_inventory_reserved_qty"]
        ),
        "production_required_qty": int(
            updated["production_required_qty"]
        ),
        "requisition_qty": int(updated["requisition_qty"]),
        "fully_covered_by_finished_inventory": bool(
            updated["fully_covered_by_finished_inventory"]
        ),
        "reservations": [
            {
                "reservation_id": row.id,
                "inventory_lot_id": row.inventory_lot_id,
                "allocated_quantity": int(
                    row.credited_requirement_quantity or 0
                ),
            }
            for row in repeated
        ],
        "late_finished_inventory": current_preview,
        "idempotent_replay": True,
        "message": "该成品库存预占已处理，本次未重复预占",
    }


@router.post("/pending/{item_id}/auto-use-finished-inventory")
def auto_use_late_finished_inventory(
    item_id: int,
    payload: PendingLateFinishedInventoryAutoReservePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    """One-click FIFO reserve exact customer-owned finished stock."""

    operation_prefix = (
        "late-finished:"
        f"{hashlib.sha256(payload.idempotency_key.encode('utf-8')).hexdigest()[:24]}:"
    )
    try:
        replay = _late_finished_idempotent_replay(
            db,
            item_id=item_id,
            operation_prefix=operation_prefix,
            user=user,
        )
        if replay is not None:
            return replay
        item, order, _customer, product = _ensure_pending_order_item_for_supplier_order(
            db, item_id
        )
        _require_order_item_customer_access(db, item, user)
        if _bom_pending_component_requirements(db, item):
            raise HTTPException(
                status_code=409,
                detail="组合产品请在展开的父件与组件明细中分别使用匹配库存",
            )

        repeated = db.scalars(
            select(InventoryReservation)
            .where(
                InventoryReservation.idempotency_key.like(
                    f"{operation_prefix}%"
                )
            )
            .order_by(InventoryReservation.id)
        ).all()
        if repeated:
            return _late_finished_idempotent_replay(
                db,
                item_id=item_id,
                operation_prefix=operation_prefix,
                user=user,
            )

        preview = _late_finished_inventory_preview(
            db,
            item=item,
            order=order,
            product=product,
        )
        if preview["blocked_reason"]:
            raise HTTPException(
                status_code=409,
                detail=f"{preview['blocked_reason']}，不能改用成品库存",
            )
        if not preview["can_auto_reserve"]:
            raise HTTPException(
                status_code=409,
                detail="没有可安全自动匹配的同客户同存货编码成品库存，请刷新后重试",
            )

        remaining = int(preview["reservable_quantity"])
        allocation_rows: list[dict] = []
        for lot in _safe_late_finished_inventory_candidates(
            db,
            item=item,
            order=order,
            product=product,
        ):
            if remaining <= 0:
                break
            quantity = min(max(int(lot.quantity_available or 0), 0), remaining)
            if quantity <= 0:
                continue
            location = lot.location
            reservation = reserve_finished_inventory(
                db,
                order_item_id=item.id,
                inventory_lot_id=lot.id,
                quantity=quantity,
                expected_version=lot.version,
                operator_id=user.id,
                idempotency_key=f"{operation_prefix}{lot.id}",
                warning_acknowledged_codes=[],
            )
            allocation_rows.append(
                {
                    "reservation_id": reservation.id,
                    "inventory_lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "allocated_quantity": quantity,
                    "location_id": location.id if location is not None else None,
                    "location_code": (
                        location.location_code if location is not None else "未设置"
                    ),
                    "location_name": (
                        location.location_name
                        if location is not None
                        else "未设置库位"
                    ),
                }
            )
            remaining -= quantity

        allocated_quantity = sum(
            int(row["allocated_quantity"]) for row in allocation_rows
        )
        if allocated_quantity <= 0:
            raise HTTPException(
                status_code=409,
                detail="成品库存已发生变化，请刷新后重试",
            )
        updated = _current_requisition_summary(db, item)
        _audit(
            db,
            user=user,
            action="AUTO_USE_LATE_FINISHED_INVENTORY",
            entity_id=item.id,
            details={
                "order_item_id": item.id,
                "inventory_lot_ids": [
                    row["inventory_lot_id"] for row in allocation_rows
                ],
                "allocated_quantity": allocated_quantity,
                "finished_inventory_reserved_qty": int(
                    updated["finished_inventory_reserved_qty"]
                ),
                "production_required_qty": int(
                    updated["production_required_qty"]
                ),
                "requisition_qty": int(updated["requisition_qty"]),
            },
            description="一键使用后来入库的客户专用成品，剩余数量再报料",
        )
        db.commit()
        return {
            "order_item_id": item.id,
            "allocated_quantity": allocated_quantity,
            "finished_inventory_reserved_qty": int(
                updated["finished_inventory_reserved_qty"]
            ),
            "production_required_qty": int(
                updated["production_required_qty"]
            ),
            "requisition_qty": int(updated["requisition_qty"]),
            "fully_covered_by_finished_inventory": bool(
                updated["fully_covered_by_finished_inventory"]
            ),
            "reservations": allocation_rows,
            "late_finished_inventory": _late_finished_inventory_preview(
                db,
                item=item,
                order=order,
                product=product,
            ),
            "idempotent_replay": False,
            "message": (
                f"已按先进先出预占成品库存 {allocated_quantity} 个；"
                f"剩余 {int(updated['production_required_qty'])} 个继续报料"
            ),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        replay = _late_finished_idempotent_replay(
            db,
            item_id=item_id,
            operation_prefix=operation_prefix,
            user=user,
        )
        if replay is not None:
            return replay
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except HTTPException:
        db.rollback()
        raise


@router.post("/pending/{item_id}/auto-use-customer-board-preparation")
def auto_use_customer_board_preparation(
    item_id: int,
    payload: PendingCustomerBoardPreparationAutoCoverPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    """One-click reserve exact customer board preparation and leave only the purchase gap."""

    try:
        item, order, _customer, product = _ensure_pending_order_item_for_supplier_order(
            db, item_id
        )
        _require_order_item_customer_access(db, item, user)
        if _bom_pending_component_requirements(db, item):
            raise HTTPException(
                status_code=409,
                detail="组合产品请在展开的父件与组件明细中分别使用匹配库存",
            )
        options = _safe_customer_board_preparation_options(
            db,
            item=item,
            order=order,
            product=product,
        )
        allocated_piece_quantity = 0
        allocated_sheet_quantity = 0
        lot_ids: list[int] = []
        for option in options:
            requirement = option["requirement"]
            if requirement is None:
                requirement = save_order_item_semi_requirement(
                    db,
                    order_item_id=item.id,
                    component_type=str(option["component_type"]),
                    board_length_mm=int(option["board_length_mm"]),
                    board_width_mm=int(option["board_width_mm"]),
                    material_code=str(option["material_code"]),
                    flute_type=str(option["flute_type"]),
                    pieces_per_box=int(option["pieces_per_box"]),
                    stock_yield_per_sheet=int(option["stock_yield_per_sheet"]),
                    required_piece_quantity=int(
                        option["required_piece_quantity"]
                    ),
                    operator_id=user.id,
                )
            candidates: list[SemiFinishedCandidate] = option["candidates"]
            result = reserve_semi_finished_inventory(
                db,
                requirement_id=requirement.id,
                requested_requirement_quantity=int(
                    option["available_piece_quantity"]
                ),
                lots=[
                    SemiFinishedLotVersion(
                        lot_id=row.lot.id,
                        expected_version=row.lot.version,
                    )
                    for row in candidates
                ],
                operator_id=user.id,
                idempotency_key=(
                    f"{payload.idempotency_key}:"
                    f"{option['component_type']}"
                ),
                confirmed=True,
                override=False,
                warning_acknowledged_codes=[],
            )
            allocated_piece_quantity += int(
                result.allocated_requirement_quantity
            )
            allocated_sheet_quantity += sum(
                int(row.reserved_stock_quantity or 0)
                for row in result.reservations
            )
            lot_ids.extend(int(row.inventory_lot_id) for row in result.reservations)

        updated = _current_requisition_summary(db, item)
        if allocated_piece_quantity > 0:
            _audit(
                db,
                user=user,
                action="AUTO_USE_CUSTOMER_BOARD_PREPARATION",
                entity_id=item.id,
                details={
                    "order_item_id": item.id,
                    "inventory_lot_ids": sorted(set(lot_ids)),
                    "allocated_sheet_quantity": allocated_sheet_quantity,
                    "allocated_piece_quantity": allocated_piece_quantity,
                    "remaining_piece_quantity": int(
                        updated["remaining_required_piece_qty"]
                    ),
                    "requisition_qty": int(updated["requisition_qty"]),
                },
                description="一键使用客户专用纸板备料并按差额报料",
            )
        db.commit()
        if allocated_piece_quantity > 0:
            message = (
                f"已预占客户专用纸板备料 {allocated_sheet_quantity} 张，"
                f"可生产 {allocated_piece_quantity} 个；"
                f"本次只需再报 {int(updated['requisition_qty'])} 张"
            )
        else:
            message = "没有找到可安全自动匹配的客户专用纸板备料，报料数量未变"
        return {
            "order_item_id": item.id,
            "allocated_sheet_quantity": allocated_sheet_quantity,
            "allocated_piece_quantity": allocated_piece_quantity,
            "semi_finished_reserved_piece_qty": int(
                updated["semi_finished_reserved_piece_qty"]
            ),
            "remaining_requirement_quantity": int(
                updated["remaining_required_piece_qty"]
            ),
            "requisition_qty": int(updated["requisition_qty"]),
            "message": message,
        }
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except HTTPException:
        db.rollback()
        raise


@router.post("/supplier-orders/preview-from-pending-selection")
def preview_supplier_orders_from_pending_selection(
    payload: PendingSupplierOrderCreatePayload,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return _pending_selection_preview_groups(db, payload, _user)


def _created_supplier_orders_response(
    orders: list[SupplierRequisitionOrder],
    *,
    idempotent_replay: bool = False,
) -> dict:
    return {
        "created_orders": [
            {
                "supplier_name": order.supplier_name,
                "supplier_order_id": order.id,
                "supplier_order_number": order.order_number,
                "pdf_url": f"/api/requisition/supplier-orders/{order.id}/pdf",
                "production_print_url": (
                    f"/requisition-production-print.html?id={order.id}"
                ),
                "item_count": len(order.items),
            }
            for order in orders
        ],
        "idempotent_replay": idempotent_replay,
    }


@router.post("/supplier-orders/from-pending-selection", status_code=status.HTTP_201_CREATED)
def create_supplier_orders_from_pending_selection(
    payload: PendingSupplierOrderFinalizePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    request_keys = [
        group.request_key
        for group in payload.supplier_groups
        if group.request_key
    ]
    if request_keys:
        if len(request_keys) != len(payload.supplier_groups):
            raise HTTPException(
                status_code=400,
                detail="报料草稿请求编号不完整，请刷新草稿后重试",
            )
        if len(set(request_keys)) != len(request_keys):
            raise HTTPException(status_code=400, detail="报料草稿请求编号重复")
        existing_orders = db.scalars(
            select(SupplierRequisitionOrder)
            .options(selectinload(SupplierRequisitionOrder.items))
            .where(SupplierRequisitionOrder.request_key.in_(request_keys))
            .order_by(SupplierRequisitionOrder.id)
        ).all()
        if existing_orders:
            if len(existing_orders) == len(request_keys):
                return _created_supplier_orders_response(
                    list(existing_orders),
                    idempotent_replay=True,
                )
            raise HTTPException(
                status_code=409,
                detail="该批报料已有部分请求完成，请刷新已报料列表核对，禁止重复生成",
            )
    try:
        grouped, touched_groups = _draft_group_entries_by_purchase_lines(
            db,
            payload,
            user,
        )
        created_orders: list[SupplierRequisitionOrder] = []
        created_entries: dict[int, list[dict]] = {}
        for supplier_name, entries in grouped.items():
            created_order = _create_supplier_order_for_pending_entries(
                db,
                supplier_name=supplier_name,
                entries=entries,
                user=user,
            )
            created_orders.append(created_order)
            created_entries[created_order.id] = entries
        for group in touched_groups:
            group.status = (
                "supplier_requisition_created"
                if all(
                    item.status == "supplier_requisition_created"
                    for item in group.items
                )
                else "merged_pending"
            )
        db.flush()
        batch_id = uuid4().hex
        all_customer_ids: set[int] = set()
        all_customer_names: set[str] = set()
        for order in created_orders:
            customer_ids, customer_names = _supplier_order_audit_customers(
                db,
                order,
            )
            all_customer_ids.update(customer_ids)
            all_customer_names.update(customer_names)
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="requisition",
                action_code="requisition.supplier_order.create",
                legacy_action="CREATE_SUPPLIER_ORDER",
                resource="Requisition",
                actor=user,
                entity_type="supplier_requisition_order",
                entity_id=order.id,
                object_ref=order.order_number,
                customer_id=(customer_ids[0] if len(customer_ids) == 1 else None),
                customer_name=(
                    customer_names[0] if len(customer_names) == 1 else None
                ),
                batch_id=batch_id,
                description="生成供应商报料单",
                details={
                    "supplier_name": order.supplier_name,
                    "item_count": len(order.items),
                    "customer_ids": customer_ids,
                    "customer_names": customer_names,
                    "order_item_ids": sorted(
                        {
                            int(item.order_item_id)
                            for item in order.items
                            if item.order_item_id is not None
                        }
                    ),
                    "request_key": order.request_key,
                    "dimension_override": any(
                        bool(entry.get("dimension_override"))
                        for entry in created_entries.get(order.id, [])
                    ),
                    "quantity_override": any(
                        bool(entry.get("quantity_override"))
                        for entry in created_entries.get(order.id, [])
                    ),
                },
            )
        append_audit_event(
            db,
            event_category="business",
            result="success",
            source="web",
            module_code="requisition",
            action_code="requisition.supplier_orders.create_batch",
            legacy_action="CREATE_SUPPLIER_ORDERS",
            resource="Requisition",
            actor=user,
            entity_type="supplier_requisition_order_batch",
            entity_id=created_orders[0].id if created_orders else None,
            object_ref=(
                created_orders[0].order_number if created_orders else "empty"
            ),
            customer_id=(
                next(iter(all_customer_ids))
                if len(all_customer_ids) == 1
                else None
            ),
            customer_name=(
                next(iter(all_customer_names))
                if len(all_customer_names) == 1
                else None
            ),
            batch_id=batch_id,
            description="待报料列表按供应商合并生成供应商报料单",
            details={
                "supplier_order_ids": [order.id for order in created_orders],
                "supplier_order_numbers": [
                    order.order_number for order in created_orders
                ],
                "supplier_names": [
                    order.supplier_name for order in created_orders
                ],
                "supplier_group_count": len(payload.supplier_groups),
                "customer_ids": sorted(all_customer_ids),
                "customer_names": sorted(all_customer_names),
            },
        )
        db.commit()
        for order in created_orders:
            db.refresh(order)
        return _created_supplier_orders_response(created_orders)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        if request_keys:
            existing_orders = db.scalars(
                select(SupplierRequisitionOrder)
                .options(selectinload(SupplierRequisitionOrder.items))
                .where(SupplierRequisitionOrder.request_key.in_(request_keys))
                .order_by(SupplierRequisitionOrder.id)
            ).all()
            if len(existing_orders) == len(request_keys):
                return _created_supplier_orders_response(
                    list(existing_orders),
                    idempotent_replay=True,
                )
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
    # The formal SQLite deployment uses one application worker.  Serialize this
    # legacy write endpoint so two requests cannot both observe an item as
    # unreported before either transaction commits.
    with _SUPPLIER_ORDER_CREATE_WRITE_LOCK:
        return _create_supplier_order_locked(payload=payload, db=db, user=user)


def _create_supplier_order_locked(
    *,
    payload: SupplierOrderCreatePayload,
    db: Session,
    user: User,
) -> dict:
    if not payload.members:
        raise HTTPException(status_code=400, detail="至少需要一条明细")

    # Authorize every referenced customer before supplier/material validation.
    # Otherwise a scoped user can distinguish supplier-master validation errors
    # for an order item they are not allowed to access.
    linked_item_ids: list[int] = []
    for member in payload.members:
        if member.item_id is None:
            if user.role not in {"admin", "boss"}:
                raise HTTPException(
                    status_code=403,
                    detail="受限账号不能创建无订单明细关联的手工报料单",
                )
            continue
        linked_item_ids.append(member.item_id)
    if len(linked_item_ids) != len(set(linked_item_ids)):
        raise HTTPException(status_code=400, detail="同一订单明细不能重复报料")

    authorized_order_items: dict[int, OrderItem] = {}
    for item_id in sorted(linked_item_ids):
        order_item = db.scalar(
            select(OrderItem)
            .where(OrderItem.id == item_id)
            .with_for_update()
        )
        if order_item is None:
            raise HTTPException(status_code=404, detail="订单明细不存在")
        _require_order_item_customer_access(db, order_item, user)
        authorized_order_items[item_id] = order_item

    supplier_name = _require_active_supplier(db, payload.supplier_name)
    material = db.get(Material, payload.material_id) if payload.material_id else None
    _require_active_material_supplier(db, material)
    effective_layer_count = (
        material.layer_count
        if material is not None
        else payload.layer_count
    )
    normalized_flute, flute_error = _business_flute_error(
        effective_layer_count,
        payload.flute_type,
    )
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)

    validated_members: list[dict] = []
    for member in payload.members:
        if member.stock_deduction_qty:
            raise HTTPException(
                status_code=400,
                detail="旧库存抵扣字段已停用，真实抵扣只能来自成品库存预占",
            )
        order_item = (
            authorized_order_items.get(member.item_id)
            if member.item_id is not None
            else None
        )
        if order_item is not None:
            if _active_requisition_hold(db, order_item.id) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="订单明细正在等候报料，请先恢复到待报料",
                )
            order = db.get(Order, order_item.order_id)
            product = db.get(Product, order_item.product_id)
            if order is None or product is None:
                raise HTTPException(
                    status_code=409,
                    detail="订单或产品快照关联已失效，不能生成供应商报料单",
                )
            _require_late_finished_inventory_resolved(
                db,
                item=order_item,
                order=order,
                product=product,
            )
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
        cutting_mode = member.cutting_mode or payload.cutting_mode
        requirements = _current_requisition_summary(
            db,
            order_item,
            cutting_mode=cutting_mode,
        )
        finished_reserved_qty = int(
            requirements["finished_inventory_reserved_qty"]
        )
        production_required_qty = int(requirements["production_required_qty"])
        if production_required_qty == 0:
            raise HTTPException(
                status_code=409,
                detail="该订单明细已由成品库存全额抵扣，无需生成供应商报料单",
            )
        pieces_per_box = int(requirements["pieces_per_box"])
        required_piece_qty = int(requirements["required_piece_qty"])
        requisition_qty = int(requirements["requisition_qty"])
        validated_members.append(
            {
                "payload": member,
                "order_item": order_item,
                "production_required_qty": production_required_qty,
                "finished_reserved_qty": finished_reserved_qty,
                "pieces_per_box": pieces_per_box,
                "required_piece_qty": required_piece_qty,
                "semi_finished_reserved_piece_qty": int(
                    requirements["semi_finished_reserved_piece_qty"]
                ),
                "remaining_required_piece_qty": int(
                    requirements["remaining_required_piece_qty"]
                ),
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
        supplier_name=supplier_name,
        material_id=payload.material_id,
        layer_count=effective_layer_count,
        flute_type=normalized_flute,
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
            **_supplier_item_snapshot_values(
                db,
                oi,
                fallback_material_id=order.material_id,
                fallback_supplier_name=order.supplier_name,
                fallback_layer_count=order.layer_count,
                fallback_flute_type=order.flute_type,
            ),
            order_number=m.order_number,
            product_code=m.product_code,
            product_name=m.product_name,
            source_key=(
                _supplier_requisition_source_key(oi)
                if oi is not None
                else None
            ),
            report_length_mm=payload.report_length_mm,
            report_width_mm=payload.report_width_mm,
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
    user = _user
    q = select(SupplierRequisitionOrder).options(
        selectinload(SupplierRequisitionOrder.items)
    )
    if status_filter:
        q = q.where(SupplierRequisitionOrder.status == status_filter)
    q = _apply_supplier_order_scope(q, user, db)
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


@router.get("/reported-customer-options")
def list_reported_customer_options(
    keyword: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> list[dict]:
    """Return only customers that already occur on a reported document."""
    customer_ids = {
        int(value)
        for value in db.scalars(
            select(Order.customer_id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(
                SupplierRequisitionOrderItem,
                SupplierRequisitionOrderItem.order_item_id == OrderItem.id,
            )
            .distinct()
        ).all()
        if value is not None
    }
    customer_ids.update(
        int(value)
        for value in db.scalars(
            select(StockReplenishmentOrder.customer_id)
            .where(StockReplenishmentOrder.customer_id.is_not(None))
            .distinct()
        ).all()
        if value is not None
    )
    customer_ids.update(
        int(value)
        for value in db.scalars(
            select(StockReplenishmentOrderItem.customer_id)
            .where(StockReplenishmentOrderItem.customer_id.is_not(None))
            .distinct()
        ).all()
        if value is not None
    )
    customer_ids.update(
        int(value)
        for value in db.scalars(
            select(Order.customer_id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .join(RequisitionItem, RequisitionItem.order_item_id == OrderItem.id)
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(
                Requisition.status.notin_(
                    ["merged_pending", "supplier_requisition_created"]
                )
            )
            .distinct()
        ).all()
        if value is not None
    )
    allowed = _allowed_customer_ids(_user, db)
    if allowed is not None:
        customer_ids.intersection_update(allowed)
    if not customer_ids:
        return []
    customer_query = select(Customer).where(Customer.id.in_(customer_ids))
    normalized_keyword = (keyword or "").strip()
    if normalized_keyword:
        escaped_keyword = (
            normalized_keyword.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped_keyword}%"
        customer_query = customer_query.where(
            or_(
                Customer.name.ilike(pattern, escape="\\"),
                Customer.customer_code.ilike(pattern, escape="\\"),
            )
        )
    rows = db.scalars(
        customer_query.order_by(Customer.name.asc(), Customer.id.asc()).limit(200)
    ).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "customer_code": row.customer_code,
            "label": (
                f"{row.customer_code}｜{row.name}" if row.customer_code else row.name
            ),
        }
        for row in rows
    ]


def _build_reported_document_candidates(db: Session, user: User) -> list[dict]:
    """Build scope-safe filter projections without full response decoration."""

    documents: list[dict] = []
    allowed_customer_ids = _allowed_customer_ids(user, db)
    supplier_header_material = aliased(Material)
    supplier_item_material = aliased(Material)

    supplier_header_query = select(
        SupplierRequisitionOrder.id.label("document_id"),
        SupplierRequisitionOrder.order_number.label("document_number"),
        SupplierRequisitionOrder.supplier_name.label("supplier_name"),
        SupplierRequisitionOrder.status.label("status"),
        SupplierRequisitionOrder.created_at.label("created_at"),
        SupplierRequisitionOrder.requisition_qty.label(
            "document_requisition_qty"
        ),
        SupplierRequisitionOrder.material_id.label("document_material_id"),
        supplier_header_material.code.label("document_material_code"),
        SupplierRequisitionOrder.flute_type.label("document_flute_type"),
        SupplierRequisitionOrder.report_length_mm.label(
            "document_report_length_mm"
        ),
        SupplierRequisitionOrder.report_width_mm.label(
            "document_report_width_mm"
        ),
        SupplierRequisitionOrder.crease_type.label("document_crease_type"),
        SupplierRequisitionOrder.crease_left_mm.label(
            "document_crease_left_mm"
        ),
        SupplierRequisitionOrder.crease_middle_mm.label(
            "document_crease_middle_mm"
        ),
        SupplierRequisitionOrder.crease_right_mm.label(
            "document_crease_right_mm"
        ),
    ).outerjoin(
        supplier_header_material,
        supplier_header_material.id == SupplierRequisitionOrder.material_id,
    ).order_by(
        SupplierRequisitionOrder.created_at.desc(),
        SupplierRequisitionOrder.id.desc(),
    )
    supplier_headers = db.execute(
        _apply_supplier_order_scope_ids(
            supplier_header_query,
            allowed_customer_ids,
        )
    ).mappings().all()
    supplier_ids = [int(row["document_id"]) for row in supplier_headers]
    supplier_items_by_order: dict[int, list] = {}
    if supplier_ids:
        supplier_item_rows = db.execute(
            select(
                SupplierRequisitionOrderItem.id.label("item_id"),
                SupplierRequisitionOrderItem.supplier_order_id.label(
                    "supplier_order_id"
                ),
                SupplierRequisitionOrderItem.order_number.label("order_number"),
                SupplierRequisitionOrderItem.product_code.label("product_code"),
                SupplierRequisitionOrderItem.product_name.label("product_name"),
                SupplierRequisitionOrderItem.customer_name.label("customer_name"),
                SupplierRequisitionOrderItem.report_length_mm.label(
                    "report_length_mm"
                ),
                SupplierRequisitionOrderItem.report_width_mm.label(
                    "report_width_mm"
                ),
                SupplierRequisitionOrderItem.material_id.label("material_id"),
                supplier_item_material.code.label("item_material_code"),
                SupplierRequisitionOrderItem.material_code_snapshot.label(
                    "material_code_snapshot"
                ),
                SupplierRequisitionOrderItem.flute_type_snapshot.label(
                    "flute_type_snapshot"
                ),
                SupplierRequisitionOrderItem.requisition_qty.label(
                    "requisition_qty"
                ),
                SupplierRequisitionOrderItem.status.label("item_status"),
                SupplierRequisitionOrderItem.version.label("item_version"),
                SupplierRequisitionOrderItem.voided_at.label("item_voided_at"),
                Order.customer_id.label("customer_id"),
            )
            .outerjoin(
                OrderItem,
                OrderItem.id == SupplierRequisitionOrderItem.order_item_id,
            )
            .outerjoin(Order, Order.id == OrderItem.order_id)
            .outerjoin(
                supplier_item_material,
                supplier_item_material.id
                == SupplierRequisitionOrderItem.material_id,
            )
            .where(
                SupplierRequisitionOrderItem.supplier_order_id.in_(supplier_ids)
            )
            .order_by(
                SupplierRequisitionOrderItem.supplier_order_id,
                SupplierRequisitionOrderItem.id,
            )
        ).mappings().all()
        for item in supplier_item_rows:
            supplier_items_by_order.setdefault(
                int(item["supplier_order_id"]), []
            ).append(item)
    for header in supplier_headers:
        document_id = int(header["document_id"])
        lines = []
        for item in supplier_items_by_order.get(document_id, []):
            lines.append(
                {
                    "_item_id": int(item["item_id"]),
                    "_requisition_qty": item["requisition_qty"],
                    "customer_id": item["customer_id"],
                    "customer_name": item["customer_name"],
                    "order_number": item["order_number"],
                    "product_code": item["product_code"],
                    "product_name": item["product_name"],
                    "report_length_mm": item["report_length_mm"]
                    or header["document_report_length_mm"],
                    "report_width_mm": item["report_width_mm"]
                    or header["document_report_width_mm"],
                    "material_code": item["material_code_snapshot"]
                    or (
                        item["item_material_code"]
                        if item["material_id"]
                        else header["document_material_code"]
                    ),
                    "flute_type": item["flute_type_snapshot"]
                    or header["document_flute_type"],
                    "_item_status": item["item_status"],
                    "_item_version": item["item_version"],
                    "_item_voided_at": item["item_voided_at"],
                }
            )
        documents.append(
            {
                "source_type": "supplier_order",
                "id": document_id,
                "document_number": header["document_number"],
                "supplier_name": header["supplier_name"],
                "status": header["status"],
                "created_at": header["created_at"],
                "_requisition_qty": header["document_requisition_qty"],
                "_crease_type": header["document_crease_type"],
                "_crease_left_mm": header["document_crease_left_mm"],
                "_crease_middle_mm": header["document_crease_middle_mm"],
                "_crease_right_mm": header["document_crease_right_mm"],
                "line_items": lines,
            }
        )

    stock_order_customer = aliased(Customer)
    stock_item_customer = aliased(Customer)
    stock_item_product = aliased(Product)
    stock_policy = aliased(InventoryStockPolicy)
    stock_policy_product = aliased(Product)
    stock_headers = db.execute(
        select(
            StockReplenishmentOrder.id.label("document_id"),
            StockReplenishmentOrder.order_number.label("document_number"),
            StockReplenishmentOrder.supplier_name.label("supplier_name"),
            StockReplenishmentOrder.customer_id.label("order_customer_id"),
            StockReplenishmentOrder.status.label("status"),
            StockReplenishmentOrder.created_at.label("created_at"),
            stock_order_customer.id.label("resolved_order_customer_id"),
            stock_order_customer.name.label("order_customer_name"),
        )
        .outerjoin(
            stock_order_customer,
            stock_order_customer.id == StockReplenishmentOrder.customer_id,
        )
        .order_by(
            StockReplenishmentOrder.created_at.desc(),
            StockReplenishmentOrder.id.desc(),
        )
    ).mappings().all()
    stock_ids = [int(row["document_id"]) for row in stock_headers]
    stock_items_by_order: dict[int, list] = {}
    if stock_ids:
        stock_item_rows = db.execute(
            select(
                StockReplenishmentOrderItem.id.label("item_id"),
                StockReplenishmentOrderItem.replenishment_order_id.label(
                    "document_id"
                ),
                StockReplenishmentOrderItem.stock_policy_id.label(
                    "stock_policy_id"
                ),
                StockReplenishmentOrderItem.product_id.label("item_product_id"),
                StockReplenishmentOrderItem.customer_id.label(
                    "item_customer_id"
                ),
                StockReplenishmentOrderItem.product_code_snapshot.label(
                    "product_code"
                ),
                StockReplenishmentOrderItem.product_name_snapshot.label(
                    "product_name"
                ),
                StockReplenishmentOrderItem.material_code_snapshot.label(
                    "material_code"
                ),
                StockReplenishmentOrderItem.flute_type.label("flute_type"),
                StockReplenishmentOrderItem.report_length_mm.label(
                    "report_length_mm"
                ),
                StockReplenishmentOrderItem.report_width_mm.label(
                    "report_width_mm"
                ),
                StockReplenishmentOrderItem.crease_type.label("crease_type"),
                StockReplenishmentOrderItem.crease_left_mm.label(
                    "crease_left_mm"
                ),
                StockReplenishmentOrderItem.crease_middle_mm.label(
                    "crease_middle_mm"
                ),
                StockReplenishmentOrderItem.crease_right_mm.label(
                    "crease_right_mm"
                ),
                StockReplenishmentOrderItem.quantity.label("quantity"),
                StockReplenishmentOrderItem.stocked_quantity.label(
                    "stocked_quantity"
                ),
                stock_item_customer.id.label("resolved_item_customer_id"),
                stock_item_customer.name.label("item_customer_name"),
                stock_item_product.id.label("resolved_item_product_id"),
                stock_item_product.customer_id.label("item_product_customer_id"),
                stock_item_product.deleted_at.label("item_product_deleted_at"),
                stock_policy.id.label("resolved_stock_policy_id"),
                stock_policy.customer_id.label("policy_customer_id"),
                stock_policy.product_id.label("policy_product_id"),
                stock_policy_product.id.label("resolved_policy_product_id"),
                stock_policy_product.customer_id.label(
                    "policy_product_customer_id"
                ),
                stock_policy_product.deleted_at.label(
                    "policy_product_deleted_at"
                ),
            )
            .outerjoin(
                stock_item_customer,
                stock_item_customer.id == StockReplenishmentOrderItem.customer_id,
            )
            .outerjoin(
                stock_item_product,
                stock_item_product.id == StockReplenishmentOrderItem.product_id,
            )
            .outerjoin(
                stock_policy,
                stock_policy.id == StockReplenishmentOrderItem.stock_policy_id,
            )
            .outerjoin(
                stock_policy_product,
                stock_policy_product.id == stock_policy.product_id,
            )
            .where(
                StockReplenishmentOrderItem.replenishment_order_id.in_(stock_ids)
            )
            .order_by(
                StockReplenishmentOrderItem.replenishment_order_id,
                StockReplenishmentOrderItem.id,
            )
        ).mappings().all()
        for item in stock_item_rows:
            stock_items_by_order.setdefault(int(item["document_id"]), []).append(
                item
            )

    def projected_stock_item_customer_id(item) -> int | None:
        customer_ids: set[int | None] = set()
        if item["item_customer_id"] is not None:
            customer_ids.add(item["item_customer_id"])
        if item["item_product_id"] is not None:
            if (
                item["resolved_item_product_id"] is None
                or item["item_product_deleted_at"] is not None
            ):
                return None
            customer_ids.add(item["item_product_customer_id"])
        if item["stock_policy_id"] is not None:
            if (
                item["resolved_stock_policy_id"] is None
                or item["policy_customer_id"] is None
            ):
                return None
            policy_customer_ids: set[int | None] = {item["policy_customer_id"]}
            if item["policy_product_id"] is not None:
                if (
                    item["resolved_policy_product_id"] is None
                    or item["policy_product_deleted_at"] is not None
                ):
                    return None
                policy_customer_ids.add(item["policy_product_customer_id"])
            if len(policy_customer_ids) != 1:
                return None
            customer_ids.update(policy_customer_ids)
        return next(iter(customer_ids)) if len(customer_ids) == 1 else None

    for header in stock_headers:
        document_id = int(header["document_id"])
        projected_items = stock_items_by_order.get(document_id, [])
        item_customer_ids = {
            projected_stock_item_customer_id(item) for item in projected_items
        }
        if allowed_customer_ids is not None:
            if (
                not projected_items
                or None in item_customer_ids
                or not item_customer_ids.issubset(allowed_customer_ids)
            ):
                continue
            if header["order_customer_id"] is not None and (
                header["order_customer_id"] not in allowed_customer_ids
                or header["order_customer_id"] not in item_customer_ids
            ):
                continue
        lines = []
        for item in projected_items:
            item_customer_name = (
                item["item_customer_name"]
                if item["resolved_item_customer_id"] is not None
                else None
            )
            customer_name = (
                item_customer_name
                if item["resolved_item_customer_id"] is not None
                else header["order_customer_name"]
            )
            lines.append(
                {
                    "_item_id": item["item_id"],
                    "_quantity": item["quantity"],
                    "_stocked_quantity": item["stocked_quantity"],
                    "_crease_type": item["crease_type"],
                    "_crease_left_mm": item["crease_left_mm"],
                    "_crease_middle_mm": item["crease_middle_mm"],
                    "_crease_right_mm": item["crease_right_mm"],
                    "_item_customer_name": item_customer_name,
                    "customer_id": projected_stock_item_customer_id(item),
                    "customer_name": customer_name,
                    "order_number": None,
                    "product_code": item["product_code"],
                    "product_name": item["product_name"],
                    "report_length_mm": item["report_length_mm"],
                    "report_width_mm": item["report_width_mm"],
                    "material_code": item["material_code"],
                    "flute_type": item["flute_type"],
                }
            )
        documents.append(
            {
                "source_type": "stock_replenishment",
                "id": document_id,
                "document_number": header["document_number"],
                "supplier_name": header["supplier_name"],
                "status": header["status"],
                "created_at": header["created_at"],
                "_order_customer_name": (
                    header["order_customer_name"]
                    if header["resolved_order_customer_id"] is not None
                    else None
                ),
                "line_items": lines,
            }
        )

    legacy_query = (
        select(Requisition)
        .options(
            load_only(
                Requisition.id,
                Requisition.requisition_number,
                Requisition.supplier_name,
                Requisition.status,
                Requisition.created_at,
            ),
            selectinload(Requisition.items).load_only(
                RequisitionItem.id,
                RequisitionItem.requisition_id,
                RequisitionItem.order_item_id,
                RequisitionItem.cardboard_len,
                RequisitionItem.cardboard_width,
                RequisitionItem.material_snapshot,
                RequisitionItem.product_code_snapshot,
                RequisitionItem.product_name_snapshot,
                RequisitionItem.requisition_qty,
                RequisitionItem.required_piece_qty,
                RequisitionItem.status,
            ),
        )
        .where(Requisition.status.notin_(["merged_pending", "supplier_requisition_created"]))
        .order_by(Requisition.created_at.desc(), Requisition.id.desc())
    )
    legacy_batches = db.scalars(
        _apply_requisition_scope_ids(
            legacy_query,
            allowed_customer_ids,
        )
    ).all()
    legacy_order_item_ids = {
        item.order_item_id for batch in legacy_batches for item in batch.items
    }
    legacy_requisition_item_ids = {
        item.id for batch in legacy_batches for item in batch.items
    }
    bom_sources_by_requisition_item_id = (
        {
            source.requisition_item_id: source
            for source in db.scalars(
                select(RequisitionItemBomSource).where(
                    RequisitionItemBomSource.requisition_item_id.in_(
                        legacy_requisition_item_ids
                    )
                )
            ).all()
        }
        if legacy_requisition_item_ids
        else {}
    )
    bom_snapshot_ids = {
        int(source.sales_order_item_bom_component_id)
        for source in bom_sources_by_requisition_item_id.values()
    }
    bom_snapshots_by_id = (
        {
            snapshot.id: snapshot
            for snapshot in db.scalars(
                select(SalesOrderItemBomComponent).where(
                    SalesOrderItemBomComponent.id.in_(bom_snapshot_ids)
                )
            ).all()
        }
        if bom_snapshot_ids
        else {}
    )
    legacy_order_rows = (
        {
            int(row["order_item_id"]): row
            for row in db.execute(
                select(
                    OrderItem.id.label("order_item_id"),
                    Order.id.label("order_id"),
                    Order.order_number.label("raw_order_number"),
                    Order.order_date.label("order_date"),
                    Order.created_at.label("order_created_at"),
                    Order.customer_id.label("customer_id"),
                    Customer.name.label("customer_name"),
                    OrderItem.flute_type.label("flute_type"),
                    OrderItem.snapshot_crease_type.label("crease_type"),
                    OrderItem.snapshot_crease_left_mm.label("crease_left_mm"),
                    OrderItem.snapshot_crease_middle_mm.label(
                        "crease_middle_mm"
                    ),
                    OrderItem.snapshot_crease_right_mm.label(
                        "crease_right_mm"
                    ),
                )
                .join(Order, Order.id == OrderItem.order_id)
                .join(Customer, Customer.id == Order.customer_id)
                .where(OrderItem.id.in_(legacy_order_item_ids))
            ).mappings().all()
        }
        if legacy_order_item_ids
        else {}
    )
    registry = build_display_registry_for_order_ids(
        db, {int(row["order_id"]) for row in legacy_order_rows.values()}
    )
    for batch in legacy_batches:
        is_composite_bom = any(
            item.id in bom_sources_by_requisition_item_id for item in batch.items
        )
        lines = []
        for item in batch.items:
            source = bom_sources_by_requisition_item_id.get(item.id)
            snapshot = (
                bom_snapshots_by_id.get(source.sales_order_item_bom_component_id)
                if source is not None
                else None
            )
            order_row = legacy_order_rows.get(item.order_item_id)
            if snapshot is not None:
                crease_type, crease_left, crease_middle, crease_right = (
                    _bom_snapshot_crease(snapshot, source.component_type)
                )
                flute_type = snapshot.snapshot_component_flute_type
            else:
                crease_type = order_row["crease_type"] if order_row is not None else None
                crease_left = order_row["crease_left_mm"] if order_row is not None else None
                crease_middle = order_row["crease_middle_mm"] if order_row is not None else None
                crease_right = order_row["crease_right_mm"] if order_row is not None else None
                flute_type = order_row["flute_type"] if order_row is not None else None
            display_number = None
            if order_row is not None:
                display_number = display_order_number(
                    SimpleNamespace(
                        id=order_row["order_id"],
                        order_number=order_row["raw_order_number"],
                        order_date=order_row["order_date"],
                        created_at=order_row["order_created_at"],
                    ),
                    registry,
                )
            lines.append(
                {
                    "_item_id": item.id,
                    "_has_bom_source": source is not None,
                    "_component_type": source.component_type if source else None,
                    "_bom_component_id": (
                        source.sales_order_item_bom_component_id if source else None
                    ),
                    "_crease_type": crease_type,
                    "_crease_left_mm": crease_left,
                    "_crease_middle_mm": crease_middle,
                    "_crease_right_mm": crease_right,
                    "_requisition_qty": item.requisition_qty,
                    "_required_piece_qty": item.required_piece_qty,
                    "_item_status": item.status,
                    "customer_id": (
                        order_row["customer_id"] if order_row is not None else None
                    ),
                    "customer_name": (
                        order_row["customer_name"] if order_row is not None else None
                    ),
                    "order_number": display_number,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "report_length_mm": int(item.cardboard_len),
                    "report_width_mm": int(item.cardboard_width),
                    "material_code": item.material_snapshot,
                    "flute_type": flute_type,
                }
            )
        documents.append(
            {
                "source_type": (
                    "composite_bom_requisition"
                    if is_composite_bom
                    else "legacy_material_requisition"
                ),
                "id": batch.id,
                "document_number": batch.requisition_number,
                "supplier_name": batch.supplier_name,
                "status": batch.status,
                "created_at": batch.created_at,
                "_is_composite_bom": is_composite_bom,
                "line_items": lines,
            }
        )

    return documents


def _load_reported_document_candidate_page_facts(
    db: Session,
    candidates: list[dict],
) -> None:
    """Load qualification facts that are needed only for visible legacy rows."""

    requisition_item_ids = {
        int(line["_item_id"])
        for candidate in candidates
        if candidate["source_type"]
        in {"composite_bom_requisition", "legacy_material_requisition"}
        for line in candidate.get("line_items") or []
    }
    received_ids = (
        {
            int(item_id)
            for item_id in db.scalars(
                select(IncomingReceiptItem.requisition_item_id).where(
                    IncomingReceiptItem.requisition_item_id.in_(
                        requisition_item_ids
                    ),
                    IncomingReceiptItem.status == "posted",
                )
            ).all()
            if item_id is not None
        }
        if requisition_item_ids
        else set()
    )
    for candidate in candidates:
        if candidate["source_type"] not in {
            "composite_bom_requisition",
            "legacy_material_requisition",
        }:
            continue
        for line in candidate.get("line_items") or []:
            line["_received"] = int(line["_item_id"]) in received_ids


def _decorate_reported_document_candidates(candidates: list[dict]) -> list[dict]:
    """Expand already-loaded page candidates into the legacy response shape."""

    def with_match_metadata(payload: dict, candidate_line: dict) -> dict:
        payload["matched"] = bool(candidate_line.get("matched"))
        payload["matched_fields"] = list(
            candidate_line.get("matched_fields") or []
        )
        return payload

    def finish_document(payload: dict, candidate: dict) -> dict:
        payload["matched_line_count"] = int(
            candidate.get("matched_line_count") or 0
        )
        payload["header_keyword_matched"] = bool(
            candidate.get("header_keyword_matched")
        )
        return payload

    documents: list[dict] = []
    for candidate in candidates:
        source_type = candidate["source_type"]
        candidate_lines = candidate.get("line_items") or []

        if source_type == "supplier_order":
            crease_display = (
                f"{candidate['_crease_left_mm']}+{candidate['_crease_middle_mm']}+{candidate['_crease_right_mm']}"
                if candidate["_crease_type"] == "压线"
                and candidate["_crease_middle_mm"] is not None
                else candidate["_crease_type"] or "-"
            )
            line_items = [
                with_match_metadata(
                    {
                        "id": line["_item_id"],
                        "stable_id": (
                            f"supplier_order:{candidate['id']}:{line['_item_id']}"
                        ),
                        "customer_id": line.get("customer_id"),
                        "customer_name": line.get("customer_name"),
                        "order_number": line.get("order_number"),
                        "product_code": line.get("product_code"),
                        "product_name": line.get("product_name"),
                        "report_length_mm": line.get("report_length_mm"),
                        "report_width_mm": line.get("report_width_mm"),
                        "crease_display": crease_display,
                        "material_code": line.get("material_code"),
                        "flute_type": line.get("flute_type"),
                        "requisition_qty": int(line.get("_requisition_qty") or 0),
                        "unit": "张",
                        "status": (
                            line.get("_item_status")
                            if candidate["status"] == "confirmed"
                            else candidate["status"]
                        ),
                        "version": int(line.get("_item_version") or 1),
                        "voided_at": (
                            beijing_naive_to_api(line["_item_voided_at"])
                            if line.get("_item_voided_at")
                            else None
                        ),
                    },
                    line,
                )
                for line in candidate_lines
            ]
            documents.append(
                finish_document(
                    {
                        "source_type": source_type,
                        "id": candidate["id"],
                        "document_number": candidate["document_number"],
                        "supplier_name": candidate.get("supplier_name"),
                        "status": candidate["status"],
                        "incoming_status": (
                            "已作废" if candidate["status"] == "voided" else "待入库"
                        ),
                        "created_at": candidate.get("created_at"),
                        "item_count": len(candidate_lines),
                        "order_numbers": _unique_text(
                            [line.get("order_number") for line in candidate_lines]
                        ),
                        "product_codes": _unique_text(
                            [line.get("product_code") for line in candidate_lines]
                        ),
                        "customer_names": _unique_text(
                            [line.get("customer_name") for line in candidate_lines]
                        ),
                        "requisition_qty": candidate.get("_requisition_qty"),
                        "pdf_url": (
                            f"/api/requisition/supplier-orders/{candidate['id']}/pdf"
                        ),
                        "production_print_url": (
                            f"/requisition-production-print.html?id={candidate['id']}"
                        ),
                        "line_items": line_items,
                    },
                    candidate,
                )
            )
            continue

        if source_type == "stock_replenishment":
            line_items = []
            for line in candidate_lines:
                crease_display = (
                    f"{line['_crease_left_mm']}+{line['_crease_middle_mm']}+{line['_crease_right_mm']}"
                    if line["_crease_type"] == "压线"
                    and line["_crease_middle_mm"] is not None
                    else line["_crease_type"] or "-"
                )
                quantity = int(line.get("_quantity") or 0)
                stocked_quantity = int(line.get("_stocked_quantity") or 0)
                line_items.append(
                    with_match_metadata(
                        {
                            "id": line["_item_id"],
                            "stable_id": (
                                f"stock_replenishment:{candidate['id']}:{line['_item_id']}"
                            ),
                            "customer_id": line.get("customer_id"),
                            "customer_name": line.get("customer_name"),
                            "order_number": None,
                            "product_code": line.get("product_code"),
                            "product_name": line.get("product_name"),
                            "report_length_mm": line.get("report_length_mm"),
                            "report_width_mm": line.get("report_width_mm"),
                            "crease_display": crease_display,
                            "material_code": line.get("material_code"),
                            "flute_type": line.get("flute_type"),
                            "requisition_qty": quantity,
                            "received_qty": stocked_quantity,
                            "remaining_qty": max(quantity - stocked_quantity, 0),
                            "unit": "张",
                            "status": candidate["status"],
                        },
                        line,
                    )
                )
            incoming_status = {
                "confirmed": "待入库",
                "partially_stocked": "部分入库",
                "stocked": "已入库",
                "voided": "已作废",
            }.get(candidate["status"], candidate["status"])
            documents.append(
                finish_document(
                    {
                        "source_type": source_type,
                        "id": candidate["id"],
                        "document_number": candidate["document_number"],
                        "supplier_name": candidate.get("supplier_name"),
                        "status": candidate["status"],
                        "incoming_status": incoming_status,
                        "created_at": candidate.get("created_at"),
                        "item_count": len(candidate_lines),
                        "order_numbers": [],
                        "product_codes": _unique_text(
                            [
                                line.get("product_code") or line.get("product_name")
                                for line in candidate_lines
                            ]
                        ),
                        "customer_names": _unique_text(
                            [
                                line.get("_item_customer_name")
                                for line in candidate_lines
                            ]
                            + [candidate.get("_order_customer_name")]
                        ),
                        "requisition_qty": sum(
                            int(line.get("_quantity") or 0)
                            for line in candidate_lines
                        ),
                        "pdf_url": (
                            "/api/requisition/stock-replenishment/orders/"
                            f"{candidate['id']}/print"
                        ),
                        "line_items": line_items,
                        "can_void": (
                            candidate["status"] == "confirmed"
                            and all(
                                int(line.get("_stocked_quantity") or 0) == 0
                                for line in candidate_lines
                            )
                        ),
                    },
                    candidate,
                )
            )
            continue

        if source_type in {
            "composite_bom_requisition",
            "legacy_material_requisition",
        }:
            is_composite_bom = bool(candidate.get("_is_composite_bom"))
            line_items = []
            for line in candidate_lines:
                has_source = bool(line.get("_has_bom_source"))
                component_type = line.get("_component_type")
                component_label = (
                    {
                        "cover": "盖",
                        "base": "底",
                        "whole": "整片",
                    }.get(component_type, "组件")
                    if has_source
                    else None
                )
                source_key = (
                    "component:"
                    f"{line.get('_bom_component_id')}:"
                    f"{component_type}"
                    if has_source
                    else None
                )
                crease_display = (
                    f"{line['_crease_left_mm']}+{line['_crease_middle_mm']}+{line['_crease_right_mm']}"
                    if line["_crease_type"] == "压线"
                    and line["_crease_middle_mm"] is not None
                    else line["_crease_type"] or "-"
                )
                line_items.append(
                    with_match_metadata(
                        {
                            "id": line["_item_id"],
                            "stable_id": (
                                f"legacy_requisition:{candidate['id']}:{line['_item_id']}"
                            ),
                            "source_key": source_key,
                            "component_type": component_type if has_source else None,
                            "component_label": component_label,
                            "customer_id": line.get("customer_id"),
                            "customer_name": line.get("customer_name"),
                            "order_number": line.get("order_number"),
                            "product_code": line.get("product_code"),
                            "product_name": line.get("product_name"),
                            "report_length_mm": line.get("report_length_mm"),
                            "report_width_mm": line.get("report_width_mm"),
                            "crease_display": crease_display,
                            "material_code": line.get("material_code"),
                            "flute_type": line.get("flute_type"),
                            "requisition_qty": int(
                                line.get("_requisition_qty") or 0
                            ),
                            "required_piece_qty": int(
                                line.get("_required_piece_qty") or 0
                            ),
                            "received_qty": None,
                            "remaining_qty": None,
                            "unit": "张",
                            "status": (
                                "已收料"
                                if line.get("_received")
                                else line.get("_item_status")
                            ),
                            "can_void": (
                                is_composite_bom
                                and candidate["status"] == "已报料"
                                and line.get("_item_status") == "有效"
                                and not line.get("_received")
                            ),
                        },
                        line,
                    )
                )
            can_void = (
                is_composite_bom
                and candidate["status"] == "已报料"
                and all(
                    line.get("_item_status") == "有效"
                    for line in candidate_lines
                )
                and all(not line.get("_received") for line in candidate_lines)
            )
            documents.append(
                finish_document(
                    {
                        "source_type": source_type,
                        "id": candidate["id"],
                        "document_number": candidate["document_number"],
                        "supplier_name": candidate.get("supplier_name"),
                        "status": candidate["status"],
                        "incoming_status": (
                            "已作废" if candidate["status"] == "已取消" else "待入库"
                        ),
                        "created_at": candidate.get("created_at"),
                        "item_count": len(candidate_lines),
                        "order_numbers": _unique_text(
                            [line.get("order_number") for line in candidate_lines]
                        ),
                        "product_codes": _unique_text(
                            [line.get("product_code") for line in candidate_lines]
                        ),
                        "customer_names": _unique_text(
                            [line.get("customer_name") for line in candidate_lines]
                        ),
                        "requisition_qty": sum(
                            int(line.get("_requisition_qty") or 0)
                            for line in candidate_lines
                        ),
                        "pdf_url": f"/requisition-print.html?id={candidate['id']}",
                        "is_composite_bom": is_composite_bom,
                        "can_void": can_void,
                        "line_items": line_items,
                    },
                    candidate,
                )
            )
            continue

        raise HTTPException(
            status_code=409,
            detail="已报料列表数据已变化，请刷新重试",
        )

    return documents


def _build_reported_documents(
    db: Session,
    user: User,
    *,
    selected_identities: set[tuple[str, int]] | None = None,
) -> list[dict]:
    """Fully decorate reported documents, optionally for selected identities only."""

    if selected_identities is not None and not selected_identities:
        return []

    def selected_ids(source: str) -> set[int] | None:
        if selected_identities is None:
            return None
        return {
            int(document_id)
            for source_type, document_id in selected_identities
            if source_type == source
        }

    registry = build_display_registry(db) if selected_identities is None else None
    documents: list[dict] = []

    supplier_ids = selected_ids("supplier_order")
    supplier_order_query = select(SupplierRequisitionOrder).options(
        selectinload(SupplierRequisitionOrder.items)
    )
    if supplier_ids is not None:
        supplier_order_query = supplier_order_query.where(
            SupplierRequisitionOrder.id.in_(supplier_ids)
        )
    supplier_orders = (
        db.scalars(
            _apply_supplier_order_scope(supplier_order_query, user, db).order_by(
                SupplierRequisitionOrder.created_at.desc(),
                SupplierRequisitionOrder.id.desc(),
            )
        ).all()
        if supplier_ids is None or supplier_ids
        else []
    )
    supplier_customer_ids: dict[int, set[int]] = {}
    supplier_item_customer_ids: dict[int, int] = {}
    supplier_order_ids = [order.id for order in supplier_orders if order.items]
    if supplier_order_ids:
        for supplier_item_id, supplier_order_id, linked_customer_id in db.execute(
            select(
                SupplierRequisitionOrderItem.id,
                SupplierRequisitionOrderItem.supplier_order_id,
                Order.customer_id,
            )
            .join(
                OrderItem,
                OrderItem.id == SupplierRequisitionOrderItem.order_item_id,
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                SupplierRequisitionOrderItem.supplier_order_id.in_(
                    supplier_order_ids
                )
            )
        ).all():
            supplier_customer_ids.setdefault(supplier_order_id, set()).add(
                linked_customer_id
            )
            supplier_item_customer_ids[supplier_item_id] = linked_customer_id
    material_ids = {
        int(material_id)
        for order in supplier_orders
        for material_id in [order.material_id, *[item.material_id for item in order.items]]
        if material_id is not None
    }
    material_codes = (
        {
            material.id: material.code
            for material in db.scalars(
                select(Material).where(Material.id.in_(material_ids))
            ).all()
        }
        if material_ids
        else {}
    )
    for order in supplier_orders:
        order_numbers = _unique_text([item.order_number for item in order.items])
        product_codes = _unique_text([item.product_code for item in order.items])
        customer_names = _unique_text([item.customer_name for item in order.items])
        crease_display = (
            f"{order.crease_left_mm}+{order.crease_middle_mm}+{order.crease_right_mm}"
            if order.crease_type == "压线" and order.crease_middle_mm is not None
            else order.crease_type or "-"
        )
        line_items = [
            {
                "id": item.id,
                "stable_id": f"supplier_order:{order.id}:{item.id}",
                "customer_id": supplier_item_customer_ids.get(item.id),
                "customer_name": item.customer_name,
                "order_number": item.order_number,
                "product_code": item.product_code,
                "product_name": item.product_name,
                "report_length_mm": item.report_length_mm or order.report_length_mm,
                "report_width_mm": item.report_width_mm or order.report_width_mm,
                "crease_display": crease_display,
                "material_code": item.material_code_snapshot
                or material_codes.get(item.material_id or order.material_id),
                "flute_type": item.flute_type_snapshot or order.flute_type,
                "requisition_qty": int(item.requisition_qty or 0),
                "unit": "张",
                "status": (
                    item.status if order.status == "confirmed" else order.status
                ),
                "version": int(item.version or 1),
                "voided_at": (
                    beijing_naive_to_api(item.voided_at)
                    if item.voided_at
                    else None
                ),
            }
            for item in order.items
        ]
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
                "production_print_url": (
                    f"/requisition-production-print.html?id={order.id}"
                ),
                "line_items": line_items,
                "_customer_ids": supplier_customer_ids.get(order.id, set()),
            }
        )

    stock_ids = selected_ids("stock_replenishment")
    stock_query = _replenishment_order_query().order_by(
        StockReplenishmentOrder.created_at.desc(),
        StockReplenishmentOrder.id.desc(),
    )
    if stock_ids is not None:
        stock_query = stock_query.where(StockReplenishmentOrder.id.in_(stock_ids))
    stock_orders = (
        db.scalars(stock_query).all() if stock_ids is None or stock_ids else []
    )
    for order in stock_orders:
        if not _stock_replenishment_order_is_visible(
            db, order, user, relationships_loaded=True
        ):
            continue
        product_codes = _unique_text(
            [item.product_code_snapshot or item.product_name_snapshot for item in order.items]
        )
        customer_names = _unique_text(
            [item.customer.name if item.customer else None for item in order.items]
            + [order.customer.name if order.customer else None]
        )
        incoming_status = {
            "confirmed": "待入库",
            "partially_stocked": "部分入库",
            "stocked": "已入库",
            "voided": "已作废",
        }.get(order.status, order.status)
        line_items = []
        for item in order.items:
            item_customer_id = _stock_replenishment_item_customer_id(
                db, item, relationships_loaded=True
            )
            item_customer = item.customer or order.customer
            crease_display = (
                f"{item.crease_left_mm}+{item.crease_middle_mm}+{item.crease_right_mm}"
                if item.crease_type == "压线" and item.crease_middle_mm is not None
                else item.crease_type or "-"
            )
            line_items.append(
                {
                    "id": item.id,
                    "stable_id": f"stock_replenishment:{order.id}:{item.id}",
                    "customer_id": item_customer_id,
                    "customer_name": item_customer.name if item_customer else None,
                    "order_number": None,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "report_length_mm": item.report_length_mm,
                    "report_width_mm": item.report_width_mm,
                    "crease_display": crease_display,
                    "material_code": item.material_code_snapshot,
                    "flute_type": item.flute_type,
                    "requisition_qty": int(item.quantity or 0),
                    "received_qty": int(item.stocked_quantity or 0),
                    "remaining_qty": max(
                        int(item.quantity or 0) - int(item.stocked_quantity or 0), 0
                    ),
                    "unit": "张",
                    "status": order.status,
                }
            )
        documents.append(
            {
                "source_type": "stock_replenishment",
                "id": order.id,
                "document_number": order.order_number,
                "supplier_name": order.supplier_name,
                "status": order.status,
                "incoming_status": incoming_status,
                "created_at": order.created_at,
                "item_count": len(order.items),
                "order_numbers": [],
                "product_codes": product_codes,
                "customer_names": customer_names,
                "requisition_qty": sum(item.quantity for item in order.items),
                "pdf_url": f"/api/requisition/stock-replenishment/orders/{order.id}/print",
                "line_items": line_items,
                "can_void": (
                    order.status == "confirmed"
                    and all(int(item.stocked_quantity or 0) == 0 for item in order.items)
                ),
                "_customer_ids": {
                    customer_id
                    for customer_id in [
                        order.customer_id,
                        *[
                            _stock_replenishment_item_customer_id(
                                db,
                                item,
                                relationships_loaded=True,
                            )
                            for item in order.items
                        ],
                    ]
                    if customer_id is not None
                },
            }
        )

    legacy_ids = None
    if selected_identities is not None:
        legacy_ids = selected_ids("legacy_material_requisition") or set()
        legacy_ids.update(selected_ids("composite_bom_requisition") or set())
    legacy_query = (
        select(Requisition)
        .options(selectinload(Requisition.items))
        .where(Requisition.status.notin_(["merged_pending", "supplier_requisition_created"]))
        .order_by(Requisition.created_at.desc(), Requisition.id.desc())
    )
    if legacy_ids is not None:
        legacy_query = legacy_query.where(Requisition.id.in_(legacy_ids))
    legacy_batches = (
        db.scalars(_apply_requisition_scope(legacy_query, user, db)).all()
        if legacy_ids is None or legacy_ids
        else []
    )
    legacy_order_item_ids = {
        item.order_item_id for batch in legacy_batches for item in batch.items
    }
    legacy_requisition_item_ids = {
        item.id for batch in legacy_batches for item in batch.items
    }
    bom_sources_by_requisition_item_id = (
        {
            source.requisition_item_id: source
            for source in db.scalars(
                select(RequisitionItemBomSource).where(
                    RequisitionItemBomSource.requisition_item_id.in_(
                        legacy_requisition_item_ids
                    )
                )
            ).all()
        }
        if legacy_requisition_item_ids
        else {}
    )
    bom_snapshot_ids = {
        int(source.sales_order_item_bom_component_id)
        for source in bom_sources_by_requisition_item_id.values()
    }
    bom_snapshots_by_id = (
        {
            snapshot.id: snapshot
            for snapshot in db.scalars(
                select(SalesOrderItemBomComponent).where(
                    SalesOrderItemBomComponent.id.in_(bom_snapshot_ids)
                )
            ).all()
        }
        if bom_snapshot_ids
        else {}
    )
    received_requisition_item_ids = (
        {
            int(item_id)
            for item_id in db.scalars(
                select(IncomingReceiptItem.requisition_item_id).where(
                    IncomingReceiptItem.requisition_item_id.in_(
                        legacy_requisition_item_ids
                    ),
                    IncomingReceiptItem.status == "posted",
                )
            ).all()
            if item_id is not None
        }
        if legacy_requisition_item_ids
        else set()
    )
    legacy_order_rows = (
        {
            item_id: (order, customer)
            for item_id, order, customer in db.execute(
                select(OrderItem.id, Order, Customer)
                .join(Order, Order.id == OrderItem.order_id)
                .join(Customer, Customer.id == Order.customer_id)
                .where(OrderItem.id.in_(legacy_order_item_ids))
            ).all()
        }
        if legacy_order_item_ids
        else {}
    )
    legacy_order_items = (
        {
            item.id: item
            for item in db.scalars(
                select(OrderItem).where(OrderItem.id.in_(legacy_order_item_ids))
            ).all()
        }
        if legacy_order_item_ids
        else {}
    )
    if registry is None:
        registry = build_display_registry_for_order_ids(
            db, {order.id for order, _customer in legacy_order_rows.values()}
        )
    for batch in legacy_batches:
        is_composite_bom = any(
            item.id in bom_sources_by_requisition_item_id
            for item in batch.items
        )
        batch_source_type = (
            "composite_bom_requisition"
            if is_composite_bom
            else "legacy_material_requisition"
        )
        if (
            selected_identities is not None
            and (batch_source_type, batch.id) not in selected_identities
        ):
            continue
        order_numbers: list[str | None] = []
        product_codes: list[str | None] = []
        customer_names: list[str | None] = []
        customer_ids: set[int] = set()
        total_requisition_qty = 0
        for item in batch.items:
            total_requisition_qty += int(item.requisition_qty or 0)
            product_codes.append(item.product_code_snapshot)
            order_row = legacy_order_rows.get(item.order_item_id)
            if order_row is None:
                continue
            order, customer = order_row
            order_numbers.append(display_order_number(order, registry))
            customer_names.append(customer.name)
            customer_ids.add(customer.id)
        line_items = []
        for item in batch.items:
            source = bom_sources_by_requisition_item_id.get(item.id)
            snapshot = (
                bom_snapshots_by_id.get(source.sales_order_item_bom_component_id)
                if source is not None
                else None
            )
            component_label = None
            source_key = None
            if source is not None:
                component_label = {
                    "cover": "盖",
                    "base": "底",
                    "whole": "整片",
                }.get(source.component_type, "组件")
                source_key = (
                    "component:"
                    f"{source.sales_order_item_bom_component_id}:"
                    f"{source.component_type}"
                )
            order_row = legacy_order_rows.get(item.order_item_id)
            order_item = legacy_order_items.get(item.order_item_id)
            order = order_row[0] if order_row else None
            customer = order_row[1] if order_row else None
            if snapshot is not None:
                crease_type, crease_left, crease_middle, crease_right = (
                    _bom_snapshot_crease(snapshot, source.component_type)
                )
                flute_type = snapshot.snapshot_component_flute_type
            else:
                crease_type = order_item.snapshot_crease_type if order_item else None
                crease_left = order_item.snapshot_crease_left_mm if order_item else None
                crease_middle = order_item.snapshot_crease_middle_mm if order_item else None
                crease_right = order_item.snapshot_crease_right_mm if order_item else None
                flute_type = order_item.flute_type if order_item else None
            crease_display = (
                f"{crease_left}+{crease_middle}+{crease_right}"
                if crease_type == "压线" and crease_middle is not None
                else crease_type or "-"
            )
            line_items.append(
                {
                    "id": item.id,
                    "stable_id": f"legacy_requisition:{batch.id}:{item.id}",
                    "source_key": source_key,
                    "component_type": source.component_type if source else None,
                    "component_label": component_label,
                    "customer_id": customer.id if customer else None,
                    "customer_name": customer.name if customer else None,
                    "order_number": display_order_number(order, registry) if order else None,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "report_length_mm": int(item.cardboard_len),
                    "report_width_mm": int(item.cardboard_width),
                    "crease_display": crease_display,
                    "material_code": item.material_snapshot,
                    "flute_type": flute_type,
                    "requisition_qty": int(item.requisition_qty or 0),
                    "required_piece_qty": int(item.required_piece_qty or 0),
                    "received_qty": None,
                    "remaining_qty": None,
                    "unit": "张",
                    "status": "已收料" if item.id in received_requisition_item_ids else item.status,
                    "can_void": (
                        is_composite_bom
                        and batch.status == "已报料"
                        and item.status == "有效"
                        and item.id not in received_requisition_item_ids
                    ),
                }
            )
        can_void = (
            is_composite_bom
            and batch.status == "已报料"
            and all(item.status == "有效" for item in batch.items)
            and all(
                item.id not in received_requisition_item_ids
                for item in batch.items
            )
        )
        documents.append(
            {
                "source_type": batch_source_type,
                "id": batch.id,
                "document_number": batch.requisition_number,
                "supplier_name": batch.supplier_name,
                "status": batch.status,
                "incoming_status": (
                    "已作废" if batch.status == "已取消" else "待入库"
                ),
                "created_at": batch.created_at,
                "item_count": len(batch.items),
                "order_numbers": _unique_text(order_numbers),
                "product_codes": _unique_text(product_codes),
                "customer_names": _unique_text(customer_names),
                "requisition_qty": total_requisition_qty,
                "pdf_url": f"/requisition-print.html?id={batch.id}",
                "is_composite_bom": is_composite_bom,
                "can_void": can_void,
                "line_items": line_items,
                "_customer_ids": customer_ids,
            }
        )

    return documents


@router.get("/reported-documents")
def list_reported_documents(
    customer_id: int | None = None,
    keyword: str | None = None,
    document_number: str | None = None,
    order_number: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    material_code: str | None = None,
    flute_type: str | None = None,
    report_length_mm: int | None = Query(default=None, ge=1),
    report_width_mm: int | None = Query(default=None, ge=1),
    report_length_min: int | None = Query(default=None, ge=1),
    report_length_max: int | None = Query(default=None, ge=1),
    report_width_min: int | None = Query(default=None, ge=1),
    report_width_max: int | None = Query(default=None, ge=1),
    supplier_name: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    source_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """List reported documents after scope filtering, with optional paging.

    This endpoint deliberately keeps an unpaged default for existing clients.
    Callers that opt into ``page`` or ``page_size`` receive one stable page only
    after all source-specific customer-scope checks and summary filters run.
    """
    # Preserve the long-standing direct-call test contract.  FastAPI normally
    # resolves Query defaults before entry; direct Python callers receive the
    # Param objects themselves and should be treated as if the values were absent.
    status_filter = status_filter if isinstance(status_filter, str) else None
    report_length_mm = report_length_mm if isinstance(report_length_mm, int) else None
    report_width_mm = report_width_mm if isinstance(report_width_mm, int) else None
    report_length_min = report_length_min if isinstance(report_length_min, int) else None
    report_length_max = report_length_max if isinstance(report_length_max, int) else None
    report_width_min = report_width_min if isinstance(report_width_min, int) else None
    report_width_max = report_width_max if isinstance(report_width_max, int) else None
    page = page if isinstance(page, int) else None
    page_size = page_size if isinstance(page_size, int) else None
    user = _user
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    allowed_source_types = {
        "supplier_order",
        "stock_replenishment",
        "composite_bom_requisition",
        "legacy_material_requisition",
    }
    if source_type and source_type not in allowed_source_types:
        raise HTTPException(status_code=422, detail="不支持的报料来源类型")

    normalized_filters = {
        "keyword": (keyword or "").strip().casefold(),
        "document_number": (document_number or "").strip().casefold(),
        "order_number": (order_number or "").strip().casefold(),
        "product_code": (product_code or "").strip().casefold(),
        "product_name": (product_name or "").strip().casefold(),
        "material_code": (material_code or "").strip().casefold(),
        "flute_type": (flute_type or "").strip().casefold(),
        "supplier_name": (supplier_name or "").strip().casefold(),
        "status": (status_filter or "").strip(),
    }
    if (
        report_length_min is not None
        and report_length_max is not None
        and report_length_min > report_length_max
    ) or (
        report_width_min is not None
        and report_width_max is not None
        and report_width_min > report_width_max
    ):
        raise HTTPException(status_code=422, detail="尺寸最小值不能大于最大值")

    def _text_contains(value: object, needle: str) -> bool:
        return needle in str(value or "").casefold()

    def _dimension_matches(
        value: object,
        exact: int | None,
        minimum: int | None,
        maximum: int | None,
    ) -> bool:
        if exact is None and minimum is None and maximum is None:
            return True
        try:
            numeric_value = int(value)
        except (TypeError, ValueError):
            return False
        if exact is not None and numeric_value != exact:
            return False
        if minimum is not None and numeric_value < minimum:
            return False
        if maximum is not None and numeric_value > maximum:
            return False
        return True

    def _line_match_fields(line: dict, *, include_keyword: bool) -> list[str] | None:
        matched_fields: list[str] = []
        if customer_id is not None:
            if line.get("customer_id") != customer_id:
                return None
            matched_fields.append("customer_id")
        for field_name in (
            "order_number",
            "product_code",
            "product_name",
            "material_code",
            "flute_type",
        ):
            needle = normalized_filters[field_name]
            if needle:
                if not _text_contains(line.get(field_name), needle):
                    return None
                matched_fields.append(field_name)
        if not _dimension_matches(
            line.get("report_length_mm"),
            report_length_mm,
            report_length_min,
            report_length_max,
        ):
            return None
        if any(
            value is not None
            for value in (report_length_mm, report_length_min, report_length_max)
        ):
            matched_fields.append("report_length_mm")
        if not _dimension_matches(
            line.get("report_width_mm"),
            report_width_mm,
            report_width_min,
            report_width_max,
        ):
            return None
        if any(
            value is not None
            for value in (report_width_mm, report_width_min, report_width_max)
        ):
            matched_fields.append("report_width_mm")
        if include_keyword and normalized_filters["keyword"]:
            keyword_fields = [
                "customer_name",
                "order_number",
                "product_code",
                "product_name",
                "material_code",
                "flute_type",
            ]
            keyword_matches = [
                field_name
                for field_name in keyword_fields
                if _text_contains(line.get(field_name), normalized_filters["keyword"])
            ]
            if not keyword_matches:
                return None
            matched_fields.extend(keyword_matches)
        return list(dict.fromkeys(matched_fields))

    has_line_filters = bool(
        customer_id is not None
        or any(
            normalized_filters[field_name]
            for field_name in (
                "order_number",
                "product_code",
                "product_name",
                "material_code",
                "flute_type",
            )
        )
        or any(
            value is not None
            for value in (
                report_length_mm,
                report_width_mm,
                report_length_min,
                report_length_max,
                report_width_min,
                report_width_max,
            )
        )
    )

    def matches_filters(document: dict) -> bool:
        if source_type and document["source_type"] != source_type:
            return False
        if normalized_filters["status"] and document["status"] != normalized_filters["status"]:
            return False
        created_at = document.get("created_at")
        created_date = created_at.date() if created_at else None
        if date_from is not None and (created_date is None or created_date < date_from):
            return False
        if date_to is not None and (created_date is None or created_date > date_to):
            return False

        if (
            normalized_filters["document_number"]
            and normalized_filters["document_number"]
            not in str(document.get("document_number") or "").casefold()
        ):
            return False
        if (
            normalized_filters["supplier_name"]
            and normalized_filters["supplier_name"]
            not in str(document.get("supplier_name") or "").casefold()
        ):
            return False
        header_keyword_match = bool(
            normalized_filters["keyword"]
            and any(
                _text_contains(value, normalized_filters["keyword"])
                for value in (
                    document.get("document_number"),
                    document.get("supplier_name"),
                )
            )
        )
        matching_lines: list[dict] = []
        for line in document.get("line_items", []):
            match_fields = _line_match_fields(
                line,
                include_keyword=bool(
                    normalized_filters["keyword"] and not header_keyword_match
                ),
            )
            if match_fields is None:
                line["matched"] = False
                line["matched_fields"] = []
                continue
            line["matched"] = True
            line["matched_fields"] = match_fields
            matching_lines.append(line)
        if has_line_filters and not matching_lines:
            return False
        if normalized_filters["keyword"] and not header_keyword_match and not matching_lines:
            return False
        document["matched_line_count"] = len(matching_lines)
        document["header_keyword_matched"] = header_keyword_match
        return True

    use_pagination = page is not None or page_size is not None
    effective_page = page or 1
    effective_page_size = page_size or 50
    if use_pagination:
        candidates = [
            document
            for document in _build_reported_document_candidates(db, user)
            if matches_filters(document)
        ]
        candidates.sort(
            key=lambda row: (
                row["created_at"] or datetime.min,
                row["id"],
            ),
            reverse=True,
        )
        total = len(candidates)
        matched_line_count = sum(
            int(document.get("matched_line_count") or 0)
            for document in candidates
        )
        start = (effective_page - 1) * effective_page_size
        page_candidates = candidates[start : start + effective_page_size]
        expected_identities = [
            (str(document["source_type"]), int(document["id"]))
            for document in page_candidates
        ]
        _load_reported_document_candidate_page_facts(db, page_candidates)
        full_documents = _decorate_reported_document_candidates(
            page_candidates
        )
        actual_identities = [
            (str(document["source_type"]), int(document["id"]))
            for document in full_documents
        ]
        if actual_identities != expected_identities:
            raise HTTPException(
                status_code=409,
                detail="已报料列表数据已变化，请刷新重试",
            )
        documents = full_documents
        if any(not matches_filters(document) for document in documents):
            raise HTTPException(
                status_code=409,
                detail="已报料列表数据已变化，请刷新重试",
            )
        return {
            "total": total,
            "matched_line_count": matched_line_count,
            "page": effective_page,
            "page_size": effective_page_size,
            "items": [
                {
                    **{
                        key: value
                        for key, value in document.items()
                        if key != "_customer_ids"
                    },
                    "created_at": (
                        utc_naive_to_api(document["created_at"])
                        if document["created_at"]
                        else None
                    ),
                }
                for document in documents
            ],
        }

    documents = _build_reported_documents(db, user)

    documents = [document for document in documents if matches_filters(document)]
    documents.sort(
        key=lambda row: (
            row["created_at"] or datetime.min,
            row["id"],
        ),
        reverse=True,
    )
    total = len(documents)
    matched_line_count = sum(
        int(document.get("matched_line_count") or 0) for document in documents
    )
    use_pagination = page is not None or page_size is not None
    effective_page = page or 1
    effective_page_size = page_size or 50
    if use_pagination:
        start = (effective_page - 1) * effective_page_size
        documents = documents[start : start + effective_page_size]
    return {
        "total": total,
        "matched_line_count": matched_line_count,
        "page": effective_page,
        "page_size": effective_page_size if use_pagination else total,
        "items": [
            {
                **{
                    key: value
                    for key, value in document.items()
                    if key != "_customer_ids"
                },
                "created_at": (
                    utc_naive_to_api(document["created_at"])
                    if document["created_at"]
                    else None
                ),
            }
            for document in documents
        ],
    }


@router.get("/reported-items")
def list_reported_items(
    customer_id: int | None = None,
    keyword: str | None = None,
    document_number: str | None = None,
    order_number: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    material_code: str | None = None,
    flute_type: str | None = None,
    report_length_mm: int | None = Query(default=None, ge=1),
    report_width_mm: int | None = Query(default=None, ge=1),
    supplier_name: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    source_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: Literal[
        "reported_at", "report_length_mm", "report_width_mm", "requisition_qty"
    ] = "reported_at",
    sort_direction: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """Return one scope-safe reported physical line per paging unit.

    The legacy ``reported-documents`` endpoint remains intact for existing
    clients.  This projection flattens its lightweight candidates before
    sorting/paging, then decorates only documents represented on the current
    item page.  It never reconstructs historical dimensions from live product
    master data.
    """

    user = _user
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    allowed_source_types = {
        "supplier_order",
        "stock_replenishment",
        "composite_bom_requisition",
        "legacy_material_requisition",
    }
    if source_type and source_type not in allowed_source_types:
        raise HTTPException(status_code=422, detail="不支持的报料来源类型")

    normalized = {
        "keyword": str(keyword or "").strip().casefold(),
        "document_number": str(document_number or "").strip().casefold(),
        "order_number": str(order_number or "").strip().casefold(),
        "product_code": str(product_code or "").strip().casefold(),
        "product_name": str(product_name or "").strip().casefold(),
        "material_code": str(material_code or "").strip().casefold(),
        "flute_type": str(flute_type or "").strip().casefold(),
        "supplier_name": str(supplier_name or "").strip().casefold(),
        "status": str(status_filter or "").strip(),
    }

    candidates = _build_reported_document_candidates(db, user)
    customer_ids = {
        int(line["customer_id"])
        for candidate in candidates
        for line in candidate.get("line_items") or []
        if line.get("customer_id") is not None
    }
    customer_names = (
        {
            int(row.id): {
                "customer_name": row.name,
                "customer_short_name": row.chinese_short_name or row.name,
            }
            for row in db.scalars(
                select(Customer).where(Customer.id.in_(customer_ids))
            ).all()
        }
        if customer_ids
        else {}
    )

    def contains(value: object, needle: str) -> bool:
        return needle in str(value or "").casefold()

    def same_dimension(value: object, expected: int | None) -> bool:
        if expected is None:
            return True
        try:
            return int(value) == expected
        except (TypeError, ValueError):
            return False

    item_candidates: list[dict] = []
    for document in candidates:
        if source_type and document["source_type"] != source_type:
            continue
        if (
            normalized["status"]
            and document["source_type"] != "supplier_order"
            and document.get("status") != normalized["status"]
        ):
            continue
        created_at = document.get("created_at")
        created_date = created_at.date() if created_at else None
        if date_from is not None and (created_date is None or created_date < date_from):
            continue
        if date_to is not None and (created_date is None or created_date > date_to):
            continue
        if normalized["document_number"] and not contains(
            document.get("document_number"), normalized["document_number"]
        ):
            continue
        if normalized["supplier_name"] and not contains(
            document.get("supplier_name"), normalized["supplier_name"]
        ):
            continue
        for line_order, line in enumerate(document.get("line_items") or [], start=1):
            effective_line_status = (
                line.get("_item_status")
                if document["source_type"] == "supplier_order"
                and document.get("status") == "confirmed"
                else document.get("status")
            )
            if normalized["status"] and effective_line_status != normalized["status"]:
                continue
            customer_projection = customer_names.get(int(line["customer_id"])) if line.get("customer_id") is not None else None
            effective_customer_name = (
                (customer_projection or {}).get("customer_name")
                or line.get("customer_name")
            )
            effective_customer_short_name = (
                (customer_projection or {}).get("customer_short_name")
                or effective_customer_name
            )
            if customer_id is not None and line.get("customer_id") != customer_id:
                continue
            if normalized["order_number"] and not contains(
                line.get("order_number"), normalized["order_number"]
            ):
                continue
            if normalized["product_code"] and not contains(
                line.get("product_code"), normalized["product_code"]
            ):
                continue
            if normalized["product_name"] and not contains(
                line.get("product_name"), normalized["product_name"]
            ):
                continue
            if normalized["material_code"] and not contains(
                line.get("material_code"), normalized["material_code"]
            ):
                continue
            if normalized["flute_type"] and not contains(
                line.get("flute_type"), normalized["flute_type"]
            ):
                continue
            if not same_dimension(line.get("report_length_mm"), report_length_mm):
                continue
            if not same_dimension(line.get("report_width_mm"), report_width_mm):
                continue
            if normalized["keyword"] and not any(
                contains(value, normalized["keyword"])
                for value in (
                    document.get("document_number"),
                    document.get("supplier_name"),
                    effective_customer_name,
                    effective_customer_short_name,
                    line.get("order_number"),
                    line.get("product_code"),
                    line.get("product_name"),
                    line.get("material_code"),
                    line.get("flute_type"),
                )
            ):
                continue
            item_candidates.append(
                {
                    "source_type": str(document["source_type"]),
                    "document_id": int(document["id"]),
                    "item_id": int(line["_item_id"]),
                    "line_order": line_order,
                    "reported_at": created_at,
                    "report_length_mm": line.get("report_length_mm"),
                    "report_width_mm": line.get("report_width_mm"),
                    "requisition_qty": int(
                        line.get("_requisition_qty")
                        or line.get("_quantity")
                        or line.get("requisition_qty")
                        or 0
                    ),
                    "customer_name": effective_customer_name,
                    "customer_short_name": effective_customer_short_name,
                    "candidate_document": document,
                }
            )

    # Stable tie order is always: latest document, newest document identity,
    # then the immutable physical line order inside that document.
    item_candidates.sort(key=lambda row: row["line_order"])
    item_candidates.sort(key=lambda row: row["document_id"], reverse=True)
    item_candidates.sort(key=lambda row: row["reported_at"] or datetime.min, reverse=True)
    if sort_by != "reported_at":
        item_candidates.sort(
            key=lambda row: int(row.get(sort_by) or 0),
            reverse=sort_direction == "desc",
        )
    elif sort_direction == "asc":
        item_candidates.sort(key=lambda row: row["reported_at"] or datetime.min)

    total = len(item_candidates)
    start = (page - 1) * page_size
    page_candidates = item_candidates[start : start + page_size]
    selected_documents: list[dict] = []
    selected_document_keys: set[tuple[str, int]] = set()
    for item_candidate in page_candidates:
        key = (item_candidate["source_type"], item_candidate["document_id"])
        if key in selected_document_keys:
            continue
        selected_document_keys.add(key)
        selected_documents.append(item_candidate["candidate_document"])
    _load_reported_document_candidate_page_facts(db, selected_documents)
    decorated_documents = _decorate_reported_document_candidates(selected_documents)
    decorated_lines = {
        (str(document["source_type"]), int(document["id"]), int(line["id"])): (
            document,
            line,
        )
        for document in decorated_documents
        for line in document.get("line_items") or []
    }

    items: list[dict] = []
    for offset, candidate in enumerate(page_candidates, start=start + 1):
        line_key = (
            candidate["source_type"],
            candidate["document_id"],
            candidate["item_id"],
        )
        decorated = decorated_lines.get(line_key)
        if decorated is None:
            raise HTTPException(
                status_code=409,
                detail="已报料明细数据已变化，请刷新重试",
            )
        document, line = decorated
        is_current_supplier_item = (
            candidate["source_type"] == "supplier_order"
            and document.get("status") == "confirmed"
            and line.get("status") == "active"
        )
        active_item_count = sum(
            1
            for document_line in document.get("line_items") or []
            if document_line.get("status") == "active"
        )
        items.append(
            {
                "sequence": offset,
                "stable_id": line.get("stable_id"),
                "source_type": candidate["source_type"],
                "document_id": candidate["document_id"],
                "document_number": document.get("document_number"),
                "item_id": candidate["item_id"],
                "line_order": candidate["line_order"],
                "order_number": line.get("order_number"),
                "customer_id": line.get("customer_id"),
                "customer_name": candidate["customer_name"],
                "customer_short_name": candidate["customer_short_name"],
                "product_code": line.get("product_code"),
                "product_name": line.get("product_name"),
                "report_length_mm": line.get("report_length_mm"),
                "report_width_mm": line.get("report_width_mm"),
                "crease_display": line.get("crease_display") or "-",
                "requisition_qty": int(line.get("requisition_qty") or 0),
                "unit": line.get("unit") or "张",
                "material_code": line.get("material_code"),
                "flute_type": line.get("flute_type"),
                "reported_at": (
                    utc_naive_to_api(candidate["reported_at"])
                    if candidate["reported_at"]
                    else None
                ),
                "supplier_name": document.get("supplier_name"),
                "status": line.get("status") or document.get("status"),
                "can_view_supplier_order": candidate["source_type"] == "supplier_order",
                "version": line.get("version"),
                "voided_at": line.get("voided_at"),
                "can_void_item": (
                    is_current_supplier_item
                    and has_permission(user, "requisition.execute")
                ),
                "can_print_task": is_current_supplier_item,
                "can_print_label": is_current_supplier_item,
                "active_item_count": active_item_count,
                "pdf_url": document.get("pdf_url"),
                "production_print_url": document.get("production_print_url"),
            }
        )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "items": items,
    }


@router.get("/supplier-orders/{order_id}")
def get_supplier_order(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    user = _user
    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    _require_supplier_order_customer_access(order, user, db)
    return _supplier_order_dict(order, db)


@router.get("/supplier-orders/{order_id}/production-print-package")
def get_supplier_order_production_print_package(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """Return the deterministic pre-receipt production print projection."""

    user = _user
    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    _require_supplier_order_customer_access(order, user, db)
    if order.status != "confirmed":
        raise HTTPException(
            status_code=409,
            detail="只有正式有效的报料单可以打印待来料生产任务单",
        )
    package = build_supplier_requisition_production_package(db, order)
    if not package["card_count"]:
        raise HTTPException(
            status_code=409,
            detail="该报料单没有可打印的正式明细",
        )
    if package["layout_overflow"]:
        raise HTTPException(
            status_code=409,
            detail="同一存货编码的物理组件超过半页容量，请先核对并拆分报料后再打印",
        )
    return package


def _apply_production_packaging_label_layout_write(
    db: Session,
    *,
    request: Request,
    user: User,
    operation_kind: Literal["save_draft", "publish", "restore_default", "rollback"],
    payload: (
        ProductionPackagingLabelLayoutDraftRequest
        | ProductionPackagingLabelLayoutReleaseRequest
    ),
    _retry_integrity: bool = True,
) -> dict:
    try:
        before = production_packaging_label_layout_admin_state(db)
        if operation_kind == "save_draft":
            if not isinstance(payload, ProductionPackagingLabelLayoutDraftRequest):
                raise ProductionPackagingLabelLayoutError("标签布局草稿请求无效")
            result = save_production_packaging_label_layout_draft(
                db,
                layout=payload.layout.model_dump(),
                expected_draft_version=payload.expected_draft_version,
                operation_key=payload.operation_key,
                actor_id=user.id,
            )
            before_layout = before["draft"]["layout"]
            after_layout = result["draft"]["layout"]
        else:
            if not isinstance(payload, ProductionPackagingLabelLayoutReleaseRequest):
                raise ProductionPackagingLabelLayoutError("标签布局发布请求无效")
            common = {
                "expected_draft_version": payload.expected_draft_version,
                "expected_release_version": payload.expected_release_version,
                "operation_key": payload.operation_key,
                "actor_id": user.id,
            }
            if operation_kind == "publish":
                result = publish_production_packaging_label_layout_draft(db, **common)
            elif operation_kind == "restore_default":
                result = restore_default_production_packaging_label_layout(db, **common)
            else:
                result = rollback_production_packaging_label_layout(db, **common)
            before_layout = before["published"]["layout"]
            after_layout = result["published"]["layout"]
        if not result["replayed"]:
            descriptions = {
                "save_draft": "保存生产包装标签布局草稿",
                "publish": "发布生产包装标签布局",
                "restore_default": "恢复并发布生产包装标签默认布局",
                "rollback": "回滚并发布上一版生产包装标签布局",
            }
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code=f"production.packaging_label_layout.{operation_kind}",
                legacy_action="PACKAGING_LABEL_LAYOUT",
                resource="ProductionPackagingLabelLayoutRevision",
                request=request,
                actor=user,
                entity_type="production_packaging_label_layout",
                object_ref="production_packaging_label_layout:global",
                batch_id=payload.operation_key,
                description=descriptions[operation_kind],
                details={
                    "operation_kind": operation_kind,
                    "draft_version": result["draft"]["version"],
                    "release_version": result["published"]["version"],
                    "layout_hash": (
                        result["draft"]["layout_hash"]
                        if operation_kind == "save_draft"
                        else result["published"]["layout_hash"]
                    ),
                    "diff": layout_diff_summary(before_layout, after_layout),
                },
            )
        db.commit()
        return result
    except ProductionPackagingLabelLayoutConflict as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProductionPackagingLabelLayoutError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        if _retry_integrity:
            return _apply_production_packaging_label_layout_write(
                db,
                request=request,
                user=user,
                operation_kind=operation_kind,
                payload=payload,
                _retry_integrity=False,
            )
        raise HTTPException(
            status_code=409,
            detail="标签布局已被其他请求更新，请重新加载",
        ) from error
    except Exception:
        db.rollback()
        raise


_PRODUCTION_PRINT_BATCH_ACTION = "requisition.production_print_batch.prepared"


def _production_print_batch_log(
    db: Session,
    batch_id: str,
) -> OperationLog | None:
    return db.scalar(
        select(OperationLog)
        .where(
            OperationLog.action_code == _PRODUCTION_PRINT_BATCH_ACTION,
            OperationLog.batch_id == batch_id,
            OperationLog.result == "success",
        )
        .order_by(OperationLog.id.desc())
    )


def _production_print_batch_details(log: OperationLog) -> dict:
    try:
        details = json.loads(log.details or "{}")
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=409,
            detail="批量打印回执损坏，已停止补打，请重新选择任务",
        ) from error
    if not isinstance(details, dict) or details.get("_truncated"):
        raise HTTPException(
            status_code=409,
            detail="批量打印回执不完整，已停止补打，请重新选择任务",
        )
    return details


def _production_print_batch_orders(
    db: Session,
    user: User,
    selections: list[dict],
) -> dict[int, SupplierRequisitionOrder]:
    order_ids = sorted(
        {int(item.get("supplier_order_id") or 0) for item in selections}
    )
    orders = {
        int(order.id): order
        for order in db.scalars(
            select(SupplierRequisitionOrder).where(
                SupplierRequisitionOrder.id.in_(order_ids)
            )
        ).all()
    }
    if len(orders) != len(order_ids):
        raise HTTPException(
            status_code=409,
            detail="所选生产任务已撤销、作废或不存在，请刷新后重新勾选",
        )
    for order_id in order_ids:
        _require_supplier_order_customer_access(orders[order_id], user, db)
    return orders


def _production_print_batch_response(
    package: dict,
    *,
    replayed: bool,
) -> dict:
    batch_id = str(package["batch_id"])
    return {
        **package,
        "replayed": replayed,
        "print_url": (
            "/requisition-production-print.html?batch_id="
            f"{batch_id}"
        ),
    }


def _raise_production_print_batch_error(error: ProductionPrintBatchError) -> None:
    detail: str | dict = str(error)
    if error.invalid_items:
        detail = {
            "code": "production_print_batch_invalid_items",
            "message": str(error),
            "invalid_items": error.invalid_items,
        }
    raise HTTPException(status_code=error.status_code, detail=detail) from error


@router.post("/production-print-batches/prepare")
def prepare_production_print_batch(
    payload: ProductionPrintBatchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
    _write_guard: None = Depends(production_print_batch_guard),
) -> dict:
    """Freeze one explicit selection without advancing any business state."""

    try:
        items = canonical_batch_items(
            [item.model_dump() for item in payload.items]
        )
        batch_id = production_print_batch_id(payload.idempotency_key)
        request_hash = production_print_batch_request_hash(items)
        existing = _production_print_batch_log(db, batch_id)
        if existing is not None:
            details = _production_print_batch_details(existing)
            if int(existing.actor_user_id_snapshot or 0) != int(user.id):
                raise HTTPException(
                    status_code=409,
                    detail="该批量打印幂等键已由其他操作员使用",
                )
            if str(details.get("request_hash") or "") != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="该批量打印幂等键已用于不同的任务选择",
                )
            stored_items = canonical_batch_items(details.get("items") or [])
            orders = _production_print_batch_orders(db, user, stored_items)
            package = build_selected_production_print_package(
                db,
                selections=stored_items,
                orders=orders,
                batch_id=batch_id,
            )
            if package["package_fingerprint"] != details.get(
                "package_fingerprint"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="已生成批次对应的生产任务内容已变化，请重新选择并生成",
                )
            return _production_print_batch_response(package, replayed=True)

        orders = _production_print_batch_orders(db, user, items)
        package = build_selected_production_print_package(
            db,
            selections=items,
            orders=orders,
            batch_id=batch_id,
        )
        append_audit_event(
            db,
            event_category="business",
            result="success",
            source="web",
            module_code="requisition",
            action_code=_PRODUCTION_PRINT_BATCH_ACTION,
            legacy_action="PREPARE_PRODUCTION_PRINT",
            resource="ProductionPrintBatch",
            actor=user,
            entity_type="production_print_batch",
            object_ref=f"production_print_batch:{batch_id}",
            batch_id=batch_id,
            description="冻结勾选的待来料生产任务打印包",
            details={
                "request_hash": request_hash,
                "items": items,
                "package_fingerprint": package["package_fingerprint"],
                "card_count": package["card_count"],
                "page_count": package["page_count"],
            },
        )
        db.commit()
        return _production_print_batch_response(package, replayed=False)
    except ProductionPrintBatchError as error:
        db.rollback()
        _raise_production_print_batch_error(error)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/production-packaging-label-layout")
def get_effective_production_packaging_label_layout(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read_production_labels),
) -> dict:
    """Return the released layout used by the next v2 print job."""

    try:
        return effective_production_packaging_label_layout(db)
    except ProductionPackagingLabelLayoutError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/production-packaging-label-layout/admin")
def get_production_packaging_label_layout_admin_state(
    db: Session = Depends(get_db),
    _user: User = Depends(admin_production_label_layout),
) -> dict:
    try:
        return production_packaging_label_layout_admin_state(db)
    except ProductionPackagingLabelLayoutError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/production-packaging-label-layout/admin/draft")
def put_production_packaging_label_layout_draft(
    payload: ProductionPackagingLabelLayoutDraftRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_production_label_layout),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    return _apply_production_packaging_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="save_draft",
        payload=payload,
    )


@router.post("/production-packaging-label-layout/admin/publish")
def post_production_packaging_label_layout_publish(
    payload: ProductionPackagingLabelLayoutReleaseRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_production_label_layout),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    return _apply_production_packaging_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="publish",
        payload=payload,
    )


@router.post("/production-packaging-label-layout/admin/restore-default")
def post_production_packaging_label_layout_restore_default(
    payload: ProductionPackagingLabelLayoutReleaseRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_production_label_layout),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    return _apply_production_packaging_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="restore_default",
        payload=payload,
    )


@router.post("/production-packaging-label-layout/admin/rollback")
def post_production_packaging_label_layout_rollback(
    payload: ProductionPackagingLabelLayoutReleaseRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_production_label_layout),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    return _apply_production_packaging_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="rollback",
        payload=payload,
    )


@router.get("/production-print-batches/{batch_id}")
def get_production_print_batch(
    batch_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    normalized_batch_id = str(batch_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_batch_id):
        raise HTTPException(status_code=404, detail="批量打印任务不存在")
    log = _production_print_batch_log(db, normalized_batch_id)
    if log is None or int(log.actor_user_id_snapshot or 0) != int(user.id):
        raise HTTPException(status_code=404, detail="批量打印任务不存在")
    details = _production_print_batch_details(log)
    try:
        items = canonical_batch_items(details.get("items") or [])
        orders = _production_print_batch_orders(db, user, items)
        package = build_selected_production_print_package(
            db,
            selections=items,
            orders=orders,
            batch_id=normalized_batch_id,
        )
    except ProductionPrintBatchError as error:
        _raise_production_print_batch_error(error)
    if package["package_fingerprint"] != details.get("package_fingerprint"):
        raise HTTPException(
            status_code=409,
            detail="批量打印包对应的任务内容已变化，请回到 ERP 重新选择",
        )
    return package


@router.get("/supplier-orders/{order_id}/production-packaging-label-package")
def get_supplier_order_production_packaging_label_package(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read_production_labels),
) -> dict:
    """Return production packaging labels without creating inventory facts."""

    user = _user
    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    _require_supplier_order_customer_access(order, user, db)
    if order.status != "confirmed":
        raise HTTPException(
            status_code=409,
            detail="只有正式有效的报料单可以打印生产包装标签",
        )
    try:
        package = build_supplier_requisition_packaging_label_package(db, order)
    except ProductionPackagingLabelLayoutError as error:
        raise HTTPException(
            status_code=409,
            detail=f"生产包装标签布局不可用：{error}",
        ) from error
    if package["review_required"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "production_label_review_required",
                "message": "生产计划已变化或标签快照不完整，请先核对并重新报料",
                "reasons": package["review_messages"],
            },
        )
    if not package["label_count"]:
        raise HTTPException(
            status_code=409,
            detail="该报料单没有启用生产包装标签的任务",
        )
    package["latest_printed_job"] = latest_printed_job_metadata(db, order.id)
    return package


@router.post("/supplier-orders/{order_id}/production-packaging-label-jobs")
def post_supplier_order_production_packaging_label_job(
    order_id: int,
    payload: ProductionPackagingLabelJobRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_production_labels),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    """Freeze a label package.  Preparing it is not an actual-print fact."""

    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    _require_supplier_order_customer_access(order, user, db)
    if order.status != "confirmed":
        raise HTTPException(status_code=409, detail="只有正式有效的报料单可创建标签打印作业")
    try:
        result = prepare_packaging_label_job(
            db,
            order=order,
            idempotency_key=payload.idempotency_key,
            expected_plan_fingerprint=payload.plan_fingerprint,
            operator_id=user.id,
        )
        # Persist the job and its exact task links atomically.  A prepared job
        # deliberately does not block a qualified task refresh.
        if not result.replayed:
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code="production.packaging_label_job.prepared",
                legacy_action="PREPARE_PRODUCTION_LABEL_JOB",
                resource="ProductionPackagingLabelPrintJob",
                actor=user,
                entity_type="production_packaging_label_print_job",
                entity_id=result.job.id,
                object_ref=f"production_packaging_label_print_job:{result.job.id}",
                batch_id=result.job.idempotency_key,
                description="冻结生产包装标签打印作业",
                details={
                    "supplier_order_id": order.id,
                    "template_version": result.job.template_version,
                    "plan_fingerprint": result.job.plan_fingerprint,
                    "payload_hash": result.job.payload_hash,
                    "label_count": result.package.get("label_count"),
                },
            )
        db.commit()
        return packaging_label_job_response(
            result.job,
            result.package,
            replayed=result.replayed,
        )
    except ProductionPackagingLabelLayoutError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"生产包装标签布局不可用：{error}",
        ) from error
    except ProductionLabelOperationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="标签打印作业已被其他请求创建，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise


@router.get("/production-packaging-label-jobs/{job_id}")
def get_production_packaging_label_job_endpoint(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_production_labels),
) -> dict:
    """Read the exact frozen payload so a historical reprint keeps its size."""

    try:
        result = get_packaging_label_job(db, job_id)
        order = db.get(SupplierRequisitionOrder, result.job.supplier_order_id)
        if order is None:
            raise ProductionLabelOperationError("打印作业关联的报料单不存在", 404)
        _require_supplier_order_customer_access(order, user, db)
        return packaging_label_job_response(result.job, result.package)
    except ProductionLabelOperationError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/production-packaging-label-jobs/{job_id}/confirm")
def confirm_production_packaging_label_job_endpoint(
    job_id: int,
    payload: ProductionPackagingLabelPrintConfirmationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_production_labels),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    """Record an actual print only after the operator explicitly confirms it."""

    try:
        existing = get_packaging_label_job(db, job_id)
        order = db.get(SupplierRequisitionOrder, existing.job.supplier_order_id)
        if order is None:
            raise ProductionLabelOperationError("打印作业关联的报料单不存在", 404)
        _require_supplier_order_customer_access(order, user, db)
        result = confirm_packaging_label_job_printed(
            db,
            job_id=job_id,
            confirmation_key=payload.idempotency_key,
            operator_id=user.id,
        )
        if not result.replayed:
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code="production.packaging_label_job.printed",
                legacy_action="CONFIRM_PRODUCTION_LABEL_PRINT",
                resource="ProductionPackagingLabelPrintJob",
                actor=user,
                entity_type="production_packaging_label_print_job",
                entity_id=result.job.id,
                object_ref=f"production_packaging_label_print_job:{result.job.id}",
                batch_id=payload.idempotency_key,
                description="人工确认生产包装标签已实际打印",
                details={
                    "supplier_order_id": result.job.supplier_order_id,
                    "template_version": result.job.template_version,
                    "plan_fingerprint": result.job.plan_fingerprint,
                    "payload_hash": result.job.payload_hash,
                },
            )
        db.commit()
        return packaging_label_job_response(
            result.job,
            result.package,
            replayed=result.replayed,
        )
    except HTTPException:
        db.rollback()
        raise
    except ProductionLabelOperationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="打印确认已被其他请求登记，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise


def _supplier_requisition_item_void_hash(
    item_id: int,
    payload: SupplierRequisitionItemVoidPayload,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "item_id": int(item_id),
                "expected_version": int(payload.expected_version),
                "confirmed": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _supplier_requisition_item_completion_id(
    db: Session,
    item: SupplierRequisitionOrderItem,
) -> int | None:
    if item.order_item_id is None:
        return None
    source_key = str(item.source_key or "").strip().lower()
    requisition_source = re.fullmatch(r"requisition_item:(\d+)", source_key)
    task_query = (
        select(ProductionCompletion.id)
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .where(
            ProductionCompletion.status == "posted",
            ProductionTask.order_item_id == item.order_item_id,
        )
    )
    if requisition_source:
        source = db.scalar(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id
                == int(requisition_source.group(1))
            )
        )
        if source is not None:
            task_query = task_query.where(
                ProductionTask.sales_order_item_bom_component_id
                == source.sales_order_item_bom_component_id
            )
    else:
        component_type = _supplier_order_item_component_type(item)
        if component_type == "whole":
            task_query = task_query.where(
                ProductionTask.sales_order_item_bom_component_id.is_(None)
            )
        elif item.product_id is not None:
            task_query = task_query.join(
                SalesOrderItemBomComponent,
                SalesOrderItemBomComponent.id
                == ProductionTask.sales_order_item_bom_component_id,
            ).where(
                SalesOrderItemBomComponent.component_product_id == item.product_id
            )
    return db.scalar(task_query.limit(1))


def _supplier_requisition_item_void_response(
    db: Session,
    item: SupplierRequisitionOrderItem,
    order: SupplierRequisitionOrder,
) -> dict:
    order_item = (
        db.get(OrderItem, item.order_item_id)
        if item.order_item_id is not None
        else None
    )
    return {
        "item": {
            "id": item.id,
            "supplier_order_id": item.supplier_order_id,
            "status": item.status,
            "version": item.version,
            "voided_at": (
                beijing_naive_to_api(item.voided_at) if item.voided_at else None
            ),
            "order_item_id": item.order_item_id,
            "source_key": item.source_key,
            "requisition_qty": int(item.requisition_qty or 0),
        },
        "supplier_order": {
            "id": order.id,
            "order_number": order.order_number,
            "status": order.status,
            "total_quantity": int(order.total_quantity or 0),
            "stock_deduction_qty": int(order.stock_deduction_qty or 0),
            "requisition_qty": int(order.requisition_qty or 0),
        },
        "order_item": (
            {
                "id": order_item.id,
                "requisition_status": order_item.requisition_status,
                "requisition_qty": order_item.requisition_qty,
                "supplier_order_number": order_item.supplier_order_number,
            }
            if order_item is not None
            else None
        ),
    }


def _recompute_supplier_order_after_item_void(
    db: Session,
    order: SupplierRequisitionOrder,
) -> list[SupplierRequisitionOrderItem]:
    active_items = list(
        db.scalars(
            select(SupplierRequisitionOrderItem)
            .where(
                SupplierRequisitionOrderItem.supplier_order_id == order.id,
                SupplierRequisitionOrderItem.status == "active",
            )
            .order_by(SupplierRequisitionOrderItem.id)
        ).all()
    )
    order.total_quantity = sum(int(row.quantity or 0) for row in active_items)
    order.stock_deduction_qty = sum(
        int(row.stock_deduction_qty or 0) for row in active_items
    )
    order.requisition_qty = sum(
        int(row.requisition_qty or 0) for row in active_items
    )
    order.required_piece_qty = sum(
        int(row.required_piece_qty or 0) for row in active_items
    )
    if active_items:
        order.status = "confirmed"
        order.voided_at = None
    else:
        order.status = "voided"
        order.voided_at = beijing_now_naive()
    return active_items


def _recompute_order_item_after_supplier_item_void(
    db: Session,
    order_item: OrderItem,
) -> None:
    active_supplier_rows = db.execute(
        select(SupplierRequisitionOrderItem, SupplierRequisitionOrder)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .where(
            SupplierRequisitionOrderItem.order_item_id == order_item.id,
            SupplierRequisitionOrderItem.status == "active",
            SupplierRequisitionOrder.status != "voided",
        )
        .order_by(
            SupplierRequisitionOrder.created_at.asc(),
            SupplierRequisitionOrder.id.asc(),
            SupplierRequisitionOrderItem.id.asc(),
        )
    ).all()
    legacy_quantity = int(
        db.scalar(
            select(func.coalesce(func.sum(RequisitionItem.requisition_qty), 0)).where(
                RequisitionItem.order_item_id == order_item.id,
                RequisitionItem.status == "有效",
            )
        )
        or 0
    )
    supplier_quantity = sum(
        int(line.requisition_qty or 0) for line, _supplier_order in active_supplier_rows
    )
    remaining_quantity = supplier_quantity + legacy_quantity
    order_item.requisition_qty = remaining_quantity or None
    if remaining_quantity > 0:
        order_item.requisition_status = "已报料"
        if active_supplier_rows:
            order_item.supplier_order_number = active_supplier_rows[-1][1].order_number
    else:
        order_item.requisition_status = "未报料"
        order_item.special_process = DEFAULT_CUTTING_MODE
        order_item.requisition_spec = None
        order_item.cardboard_len = None
        order_item.cardboard_width = None
        order_item.requisition_date = None
        order_item.supplier_delivery_time = None
        order_item.supplier_order_number = None
        order_item.requisition_remark = None


@router.put("/supplier-order-items/{item_id}/void")
def void_supplier_requisition_item(
    item_id: int,
    payload: SupplierRequisitionItemVoidPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    request_hash = _supplier_requisition_item_void_hash(item_id, payload)
    with _SUPPLIER_ORDER_ITEM_VOID_WRITE_LOCK:
        try:
            item = db.get(SupplierRequisitionOrderItem, item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="供应商报料明细不存在")
            order = db.get(SupplierRequisitionOrder, item.supplier_order_id)
            if order is None:
                raise HTTPException(status_code=409, detail="供应商报料单不存在")
            _require_supplier_order_customer_access(order, user, db)

            existing_key_item = db.scalar(
                select(SupplierRequisitionOrderItem).where(
                    SupplierRequisitionOrderItem.void_idempotency_key
                    == payload.idempotency_key
                )
            )
            if existing_key_item is not None:
                if (
                    existing_key_item.id != item.id
                    or existing_key_item.void_request_hash != request_hash
                    or existing_key_item.voided_by != user.id
                    or existing_key_item.status != "voided"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="该幂等键已用于另一笔撤销或请求内容不一致",
                    )
                return _supplier_requisition_item_void_response(db, item, order)
            if item.status != "active":
                raise HTTPException(status_code=409, detail="该报料明细已经撤销")
            if item.version != payload.expected_version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "SUPPLIER_REQUISITION_ITEM_VERSION_CONFLICT",
                        "message": "报料明细版本已变化，请刷新后重试",
                        "current_version": item.version,
                    },
                )

            claimed = db.execute(
                update(SupplierRequisitionOrderItem)
                .where(
                    SupplierRequisitionOrderItem.id == item.id,
                    SupplierRequisitionOrderItem.status == "active",
                    SupplierRequisitionOrderItem.version == payload.expected_version,
                )
                .values(version=SupplierRequisitionOrderItem.version)
            )
            if claimed.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="报料明细已被其他操作处理，请刷新后重试",
                )
            db.flush()
            db.expire_all()
            item = db.get(SupplierRequisitionOrderItem, item_id)
            order = db.get(SupplierRequisitionOrder, item.supplier_order_id)
            if item is None or order is None or item.status != "active":
                raise HTTPException(
                    status_code=409,
                    detail="报料明细已被其他操作处理，请刷新后重试",
                )
            _require_supplier_order_customer_access(order, user, db)
            if order.status != "confirmed":
                raise HTTPException(status_code=409, detail="该报料单当前不能逐明细撤销")

            receipt_id = db.scalar(
                select(IncomingReceiptItem.id)
                .where(
                    IncomingReceiptItem.supplier_order_item_id == item.id,
                    IncomingReceiptItem.status == "posted",
                )
                .limit(1)
            )
            if receipt_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"该明细已有有效来料实收 #{receipt_id}，不能撤销",
                )
            completion_id = _supplier_requisition_item_completion_id(db, item)
            if completion_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"该明细关联生产完工 #{completion_id}，不能撤销",
                )

            before = {
                "status": item.status,
                "version": item.version,
                "supplier_order_status": order.status,
                "supplier_order_requisition_qty": int(order.requisition_qty or 0),
            }
            now = beijing_now_naive()
            item.status = "voided"
            item.version += 1
            item.voided_at = now
            item.voided_by = user.id
            item.void_idempotency_key = payload.idempotency_key
            item.void_request_hash = request_hash
            db.flush()

            source_match = re.fullmatch(
                r"requisition_item:(\d+)", str(item.source_key or "").strip()
            )
            if source_match:
                remaining_same_source = db.scalar(
                    select(SupplierRequisitionOrderItem.id)
                    .join(
                        SupplierRequisitionOrder,
                        SupplierRequisitionOrder.id
                        == SupplierRequisitionOrderItem.supplier_order_id,
                    )
                    .where(
                        SupplierRequisitionOrderItem.source_key == item.source_key,
                        SupplierRequisitionOrderItem.status == "active",
                        SupplierRequisitionOrder.status != "voided",
                    )
                    .limit(1)
                )
                if remaining_same_source is None:
                    requisition_item_id = int(source_match.group(1))
                    requisition_item = db.get(RequisitionItem, requisition_item_id)
                    if requisition_item is not None:
                        requisition_item.status = "已取消"
                    db.execute(
                        update(RequisitionItemBomSource)
                        .where(
                            RequisitionItemBomSource.requisition_item_id
                            == requisition_item_id
                        )
                        .values(active_guard=None)
                    )

            active_items = _recompute_supplier_order_after_item_void(db, order)
            order_item = (
                db.get(OrderItem, item.order_item_id)
                if item.order_item_id is not None
                else None
            )
            if order_item is not None:
                _recompute_order_item_after_supplier_item_void(db, order_item)

            customer_ids, customer_names = _supplier_order_audit_customers(db, order)
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="requisition",
                action_code="requisition.supplier_order_item.void",
                legacy_action="VOID_REQUISITION_ITEM",
                resource="SupplierRequisitionOrderItem",
                request=request,
                actor=user,
                entity_type="supplier_requisition_order_item",
                entity_id=item.id,
                object_ref=f"supplier_requisition_order_item:{item.id}",
                customer_id=customer_ids[0] if len(customer_ids) == 1 else None,
                customer_name=customer_names[0] if len(customer_names) == 1 else None,
                batch_id=payload.idempotency_key,
                description="逐明细撤销供应商报料事实",
                details={
                    "before": before,
                    "after": {
                        "status": item.status,
                        "version": item.version,
                        "supplier_order_status": order.status,
                        "supplier_order_requisition_qty": int(
                            order.requisition_qty or 0
                        ),
                        "active_item_ids": [row.id for row in active_items],
                    },
                    "source_key": item.source_key,
                    "order_item_id": item.order_item_id,
                    "requisition_qty": int(item.requisition_qty or 0),
                    "customer_ids": customer_ids,
                    "customer_names": customer_names,
                },
            )
            db.commit()
            db.refresh(item)
            db.refresh(order)
            return _supplier_requisition_item_void_response(db, item, order)
        except HTTPException:
            db.rollback()
            raise
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="报料明细已被其他操作处理，请刷新后重试",
            ) from error
        except Exception:
            db.rollback()
            raise


@router.put("/supplier-orders/{order_id}/void")
def void_supplier_order(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_rollback),
) -> dict:
    order = db.get(SupplierRequisitionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="供应商报料单不存在")
    _require_supplier_order_customer_access(order, user, db)
    if order.status == "voided":
        raise HTTPException(status_code=400, detail="该报料单已作废")

    order_item_ids = {
        int(item.order_item_id)
        for item in order.items
        if item.order_item_id is not None
    }
    posted_receipt = db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            IncomingReceiptItem.status == "posted",
            or_(
                IncomingReceiptItem.supplier_order_id == order.id,
                IncomingReceiptItem.order_item_id.in_(order_item_ids),
            ),
        )
        .limit(1)
    )
    if posted_receipt is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该报料单已有来料实收记录 #{posted_receipt}，请先撤销来料实收",
        )
    posted_completion = (
        db.scalar(
            select(ProductionCompletion.id)
            .where(
                ProductionCompletion.order_item_id.in_(order_item_ids),
                ProductionCompletion.status == "posted",
            )
            .limit(1)
        )
        if order_item_ids
        else None
    )
    if posted_completion is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该报料单关联生产完工记录 #{posted_completion}，请先撤销生产完工",
        )

    before_status = order.status
    affected_items: list[dict[str, object]] = []
    order.status = "voided"
    order.voided_at = beijing_now_naive()

    for item in order.items:
        if item.order_item_id:
            oi = db.get(OrderItem, item.order_item_id)
            if oi and oi.requisition_status != "未报料":
                affected_items.append(
                    {
                        "order_item_id": oi.id,
                        "before_requisition_status": oi.requisition_status,
                        "before_inventory_deducted_qty": int(
                            oi.inventory_deducted_qty or 0
                        ),
                        "before_requisition_qty": (
                            int(oi.requisition_qty)
                            if oi.requisition_qty is not None
                            else None
                        ),
                    }
                )
                remaining_facts = _active_supplier_requisition_facts(
                    db,
                    item=oi,
                )
                remaining_qty = int(remaining_facts["quantity"])
                oi.inventory_deducted_qty = 0
                oi.requisition_qty = remaining_qty or None
                if remaining_qty > 0:
                    oi.requisition_status = "已报料"
                    latest = list(remaining_facts.get("orders") or [])[-1]
                    oi.supplier_order_number = str(
                        latest.get("supplier_order_number") or ""
                    ) or None
                else:
                    oi.requisition_status = "未报料"
                    oi.special_process = DEFAULT_CUTTING_MODE
                    oi.requisition_spec = None
                    oi.cardboard_len = None
                    oi.cardboard_width = None
                    oi.requisition_date = None
                    oi.supplier_delivery_time = None
                    oi.supplier_order_number = None
                    oi.requisition_remark = None

    try:
        if order_item_ids:
            active_requisition_ids = select(RequisitionItem.id).where(
                RequisitionItem.order_item_id.in_(order_item_ids),
                RequisitionItem.status == "有效",
            )
            db.execute(
                update(RequisitionItem)
                .where(RequisitionItem.id.in_(active_requisition_ids))
                .values(status="已取消")
            )
            db.execute(
                update(RequisitionItemBomSource)
                .where(
                    RequisitionItemBomSource.requisition_item_id.in_(
                        select(RequisitionItem.id).where(
                            RequisitionItem.order_item_id.in_(order_item_ids)
                        )
                    )
                )
                .values(active_guard=None)
            )
            release_active_finished_reservations_for_items(
                db,
                order_item_ids=sorted(order_item_ids),
                operator_id=user.id,
                reason="作废供应商报料单，自动释放成品库存预占",
                idempotency_prefix=f"void-supplier-order-{order.id}-finished",
            )
            release_active_semi_reservations_for_items(
                db,
                order_item_ids=sorted(order_item_ids),
                operator_id=user.id,
                reason="作废供应商报料单，自动释放半成品库存预占",
                idempotency_prefix=f"void-supplier-order-{order.id}-semi",
            )
        if isinstance(user, User):
            customer_ids, customer_names = _supplier_order_audit_customers(
                db,
                order,
            )
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="requisition",
                action_code="requisition.supplier_order.void",
                legacy_action="VOID_SUPPLIER_ORDER",
                resource="Requisition",
                actor=user,
                entity_type="supplier_requisition_order",
                entity_id=order.id,
                object_ref=order.order_number,
                customer_id=(
                    customer_ids[0] if len(customer_ids) == 1 else None
                ),
                customer_name=(
                    customer_names[0] if len(customer_names) == 1 else None
                ),
                description="作废供应商报料单并恢复关联订单待报料",
                details={
                    "before_status": before_status,
                    "after_status": order.status,
                    "voided_at": order.voided_at,
                    "supplier_name": order.supplier_name,
                    "customer_ids": customer_ids,
                    "customer_names": customer_names,
                    "affected_items": affected_items,
                },
            )
        db.commit()
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return _supplier_order_dict(order, db)
