from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal
from uuid import uuid4

import jwt
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only, selectinload

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
    utc_now_naive,
)
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingPurchasePurgeAuthorization,
    ExternalPackagingReceipt,
)
from app.models.finance import (
    FinanceManualMutation,
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.invoice_task import FinanceInvoiceTask
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.order_estimated_cost_snapshot import (
    SalesOrderItemEstimatedCostSnapshot,
)
from app.models.order_material_cost_snapshot import (
    SalesOrderItemMaterialCostSnapshot,
)
from app.models.product import Product
from app.models.production import ProductionCompletion
from app.models.product_bom import (
    ProductBomComponent,
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.requisition import Requisition, RequisitionHold, RequisitionItem
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
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryLot,
    InventoryReservation,
)
from app.services.order_number_display import (
    build_display_registry,
    serialize_order_number_fields,
)
from app.services.fulfillment_reminders import list_order_production_reminders
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
from app.services.order_document_trace import build_order_item_document_trace
from app.services.order_material_cost import (
    MaterialCostEstimateContext,
    build_material_cost_estimate_context,
    estimate_order_item_material_cost,
)
from app.services.order_material_cost_snapshot import (
    freeze_order_item_material_cost,
    get_latest_order_item_material_cost_snapshot,
    get_latest_order_item_material_cost_snapshots_by_items,
    mark_current_estimate_as_non_historical,
    serialize_order_item_material_cost_snapshot,
)
from app.services.order_estimated_cost_snapshot import (
    classify_estimated_cost_health,
    freeze_order_item_estimated_cost,
    get_latest_order_item_estimated_cost_snapshot,
    get_latest_order_item_estimated_cost_snapshots_by_items,
    is_estimated_cost_snapshot_unique_conflict,
    serialize_order_item_estimated_cost_snapshot,
)
from app.services.order_cost_readiness import (
    COST_GAP_CATEGORIES,
    classify_cost_gaps,
    load_cost_missing_items,
)
from app.services.audit_log import append_audit_event
from app.services.order_business_status import (
    active_order_status_badge_snapshot,
    BUSINESS_STATUS_ORDER,
    DERIVED_BUSINESS_STATUSES,
    build_order_business_statuses,
    order_status_projection_load_options,
)
from app.services.order_status_policy import (
    ALL_ORDER_STATUSES,
    FULFILLMENT_TERMINAL_ORDER_STATUSES,
    MANAGEMENT_TERMINAL_ORDER_STATUSES,
    ORDER_ITEM_ACTIVE_ORDER_STATUSES,
)
from app.services.order_customer_heat import (
    THRESHOLD_STATUS as CUSTOMER_HEAT_THRESHOLD_STATUS,
    THRESHOLD_VERSION as CUSTOMER_HEAT_THRESHOLD_VERSION,
    customer_ids_matching_heat_filters,
    list_customer_heat,
    threshold_contract as customer_heat_threshold_contract,
)
from app.services.customer_search import customer_identity_search_clause
from app.services.customer_price_tax import resolve_customer_price_tax_terms
from app.services import material_pricing
from app.services.box_type_rules import (
    BoxTypeRuleError,
    box_type_code,
    get_box_type_rule,
    normalize_box_configuration,
)
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
from app.services.product_specification import resolved_product_specification
from app.services.manual_size_product import (
    ManualSizeProductError,
    ManualSizeProductInput,
    resolve_or_create_manual_size_product,
)
from app.services.mold_location import describe_mold_location
from app.services.production_workflow import (
    ProductionWorkflowError,
    cutting_output_factor,
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
from app.services.order_external_packaging import (
    OrderExternalPackagingSnapshotError,
    freeze_order_item_external_components,
    get_order_item_external_components_by_item_ids,
)
from app.services.external_packaging_purchase import (
    get_external_purchase_summaries_by_order_ids,
    get_external_purchase_summary,
)
from app.services.external_packaging_purchase_lifecycle import (
    fully_cancelled_external_item_ids,
    ExternalPackagingPurchaseLifecycleError,
    active_external_purchase_orders_for_order_ids,
    cancel_unreceived_external_purchases,
)
from app.services.external_packaging_receiving import purchase_receipt_progress
from app.services.composite_bom_workflow import (
    CompositeBomWorkflowError,
    append_component_demand_adjustment,
    append_order_quantity_adjustments,
    ensure_component_production_tasks,
    is_composite_order_item,
)
from app.services.production_label_strategy import (
    CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
)
from app.services.report_crease import crease_width_error, product_crease_width_error
from app.services.supplier_master import SupplierLookupError, resolve_supplier
from app.services.product_drawings import (
    DrawingValidationError,
    remove_drawing_files,
    save_product_drawing_files,
)
from app.services.secure_uploads import (
    DRAWING_POLICY,
    PDF_POLICY,
    PendingTemporaryConsumption,
    UploadTokenError,
    UploadValidationError,
    create_temporary_token,
    finalize_temporary_token_consumption,
    read_validated_upload,
    resolve_stored_reference,
    rollback_temporary_token_consumption,
    stage_temporary_token_consumption,
    stored_file_metadata,
    temporary_token_file,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    active_finished_reservations_by_item_ids,
    component_inventory_coverage,
    finished_inventory_candidates_for_product,
    has_unconsumed_inventory_reservations,
    normalize_material_code,
    release_active_finished_reservations_for_items,
    reserve_finished_inventory,
)
from app.services.semi_finished_inventory import (
    CUSTOMER_GENERIC_SEMI_FINISHED_STOCK,
    GENERAL_SEMI_FINISHED_STOCK,
    SIGNATURE_OVERRIDE_WARNING,
    SemiFinishedLotVersion,
    SemiFinishedSignature,
    active_semi_coverage_by_order_item,
    browse_semi_finished_inventory,
    direct_semi_finished_deduction_eligible,
    ensure_semi_finished_lot_eligibility,
    release_active_semi_reservations_for_items,
    reserve_semi_finished_inventory,
    requirement_signature,
    save_order_item_semi_requirement,
    semi_finished_inventory_candidates,
)
from app.services.sheet_cut_plan import rectangular_cut_plan
from app.services.requisition_quantities import (
    CUTTING_MODE_BOX_STYLES,
    DEFAULT_CUTTING_MODE,
    cutting_factor,
    purchase_sheet_quantity,
    required_piece_quantity,
)
from app.services.mold_repair import (
    issue_order_mold_repair_confirmation,
    repair_warnings_for_products,
    require_order_mold_repair_confirmation,
)


router = APIRouter()
order_save_logger = logging.getLogger("erp.order_save")
can_create = PermissionChecker("orders.create")
can_read = PermissionChecker("orders.view")
can_edit = PermissionChecker("orders.edit")
can_status = PermissionChecker("orders.status")
can_delete = PermissionChecker("orders.delete")
can_rollback = PermissionChecker("orders.rollback")
can_view_cost = PermissionChecker("cost.view")

ORDER_SALES_AMOUNT_ROLES = frozenset({"admin", "boss", "sales", "finance"})


def _include_order_list_unfinished_total() -> bool:
    """Keep the public list badge while allowing internal scoped reuse to skip it."""

    return True


def _business_status_projection_load_options() -> list:
    """Load exactly the fields consumed by the read-only status projection."""

    return order_status_projection_load_options()


def _can_view_order_sales_amount(user: User) -> bool:
    """Keep customer sales amounts on an explicit, fail-closed role allowlist."""

    return user.role in ORDER_SALES_AMOUNT_ROLES and has_permission(user, "orders.view")


def _append_order_audit(
    db: Session,
    *,
    request: Request | None,
    user: User,
    order: Order,
    action_code: str,
    legacy_action: str,
    description: str,
    details: dict[str, object],
    entity_type: str = "order",
    entity_id: int | None = None,
    object_ref: str | None = None,
    resource: str = "Order",
    source: str = "web",
    batch_id: str | None = None,
) -> None:
    """Append one structured order event inside the caller's transaction."""

    customer = db.get(Customer, order.customer_id)
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source=source,
        module_code="orders",
        action_code=action_code,
        legacy_action=legacy_action,
        resource=resource,
        entity_type=entity_type,
        entity_id=order.id if entity_id is None else entity_id,
        object_ref=object_ref or order.order_number,
        customer_id=order.customer_id,
        customer_name=customer.name if customer is not None else None,
        batch_id=batch_id,
        description=description,
        details=details,
    )
_PRODUCT_DRAWING_SAVE_OPTIONS = frozenset({"save_to_product", "overwrite_product"})
MONEY_QUANTUM = Decimal("0.00")
EXTERNAL_QUANTITY_QUANTUM = Decimal("0.000001")
ORDER_STATUSES = ALL_ORDER_STATUSES
FINAL_ORDER_STATUSES = FULFILLMENT_TERMINAL_ORDER_STATUSES

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
_BUSINESS_EXCLUDED_STATUSES = MANAGEMENT_TERMINAL_ORDER_STATUSES
_DERIVED_STATUS_FILTER_GROUPS = {
    "unfinished": {
        "pending_confirmation",
        "pending_material",
        "pending_incoming",
        "pending_production",
        "pending_delivery",
        "partially_delivered",
    },
    "finished_delivery": {
        "waiting_receipt",
        "pending_reconciliation",
        "pending_invoice",
        "pending_payment",
        "completed",
    },
}
_MANUAL_ORDER_STATUS_TARGETS = frozenset(
    {"archived", "closed", "dead", "cancelled"}
)
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
        "learned", "signature", "customer_generic", "general_signature", "manual"
    ]
    match_rule_id: int | None = None
    override: bool = False
    confirmed: bool = False
    direct_deduction: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)


class OrderItemReservationPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finished: list[FinishedReservationPlanEntry] = Field(default_factory=list)
    semi: list[SemiReservationPlanEntry] = Field(default_factory=list)


class InventoryDraftPreviewItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_line_id: str = Field(min_length=1, max_length=100)
    product_id: int = Field(gt=0, strict=True)
    quantity: int = Field(gt=0, strict=True)
    material: str | None = None
    flute_type: str | None = None
    reservation_plan: OrderItemReservationPlan = Field(
        default_factory=OrderItemReservationPlan
    )
    finished_skipped: bool = False


class InventoryDraftPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    customer_id: int = Field(gt=0, strict=True)
    items: list[InventoryDraftPreviewItem] = Field(min_length=1, max_length=200)


class NewOrderBomComponentDemand(BaseModel):
    product_bom_component_id: int = Field(gt=0, strict=True)
    required_piece_quantity: int = Field(gt=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ExistingOrderBomComponentDemand(BaseModel):
    snapshot_id: int = Field(gt=0, strict=True)
    required_piece_quantity: int = Field(gt=0, strict=True)
    expected_required_piece_quantity: int = Field(gt=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class OrderItemCreate(BaseModel):
    client_line_id: str | None = Field(default=None, max_length=100)
    reservation_plan: OrderItemReservationPlan | None = None
    bom_component_demands: list[NewOrderBomComponentDemand] = Field(
        default_factory=list
    )
    product_id: int | None = None
    quantity: int | float
    unit_price: Decimal
    external_packaging_order_quantity_basis: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    external_packaging_purchase_quantity_basis: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=6
    )
    product_code: str | None = None
    product_name: str | None = None
    material: str | None = None
    original_material_code: str | None = Field(default=None, max_length=250)
    specification: str | None = None
    customer_model: str | None = None  # v0.19.1: TH型号 / 客户型号
    production_notes: str | None = None  # v0.19.2-A: 生产/印刷/打勾/摆放/日文警示等行级说明
    is_new_product: bool = False
    # P1-03: an explicit new-order-only path.  A null product_id alone never
    # means this is a hand-entered size line.
    manual_size_entry: bool = False
    box_type: str | None = Field(default=None, max_length=50)
    length_mm: int | None = Field(default=None, gt=0)
    width_mm: int | None = Field(default=None, gt=0)
    height_mm: int | None = Field(default=None, gt=0)
    report_length_mm: int | None = Field(default=None, gt=0)
    report_width_mm: int | None = Field(default=None, gt=0)
    crease_type: str | None = Field(default=None, max_length=20)
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    base_report_length_mm: int | None = Field(default=None, gt=0)
    base_report_width_mm: int | None = Field(default=None, gt=0)
    base_crease_type: str | None = Field(default=None, max_length=20)
    base_crease_left_mm: int | None = Field(default=None, ge=0)
    base_crease_middle_mm: int | None = Field(default=None, ge=0)
    base_crease_right_mm: int | None = Field(default=None, ge=0)
    splice_mode: str | None = Field(default=None, max_length=20)
    pieces_per_box: int | None = Field(default=None, ge=1)
    flap_mm: int | None = Field(default=None, gt=0)
    default_cutting_mode: str | None = Field(default=None, max_length=20)
    material_id: int | None = None
    layer_count: int | None = None   # v0.19.2-B: 常用箱层数（自动带出）
    flute_type: str | None = None    # v0.19.2-B: 常用箱实际楞型（自动带出）
    temp_drawing_token: str | None = Field(default=None, min_length=32, max_length=32)
    drawing_save_option: Literal[
        "order_only", "save_to_product", "overwrite_product"
    ] | None = None
    # 组合销售来源由服务端复核后冻结；前端不能借此把父件伪装成组件或反过来。
    combination_mode_snapshot: Literal[
        "parent_priced_set", "component_priced"
    ] | None = None
    composite_fulfillment_mode_snapshot: Literal[
        "parent_delivery", "component_delivery"
    ] | None = None
    combination_role: Literal["standalone", "set_parent", "priced_component"] | None = None
    combination_group_key: str | None = Field(default=None, max_length=80)
    combination_parent_product_id: int | None = None
    combination_parent_name_snapshot: str | None = Field(default=None, max_length=250)
    combination_set_quantity_snapshot: int | None = Field(default=None, ge=1)
    combination_quantity_per_set_snapshot: int | None = Field(default=None, ge=1)

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
    bom_production_expected_revision: int | None = Field(default=None, ge=0)
    mold_repair_confirmation_token: str | None = Field(default=None, max_length=4000)
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
    special_process: str | None = Field(default=None, max_length=30)
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
    quantity_adjustment_idempotency_key: str | None = Field(
        default=None,
        max_length=120,
    )
    bom_component_demands: list[ExistingOrderBomComponentDemand] = Field(
        default_factory=list
    )


class BomComponentDemandUpdate(BaseModel):
    required_piece_quantity: int = Field(gt=0, strict=True)
    expected_required_piece_quantity: int = Field(gt=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class EstimatedCostUpdate(BaseModel):
    expected_snapshot_version: int = Field(ge=1)
    loss_rate: Decimal = Decimal("0.03")
    die_fee: Decimal = Field(default=Decimal("0"), ge=0)
    plate_fee: Decimal = Field(default=Decimal("0"), ge=0)
    freight_fee: Decimal = Field(default=Decimal("0"), ge=0)
    other_fee: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("loss_rate")
    @classmethod
    def _validate_loss_rate(cls, value: Decimal) -> Decimal:
        if value not in {Decimal("0.03"), Decimal("0.05")}:
            raise ValueError("生产加报损耗只能选择 3% 或 5%")
        return value


class OrderCreate(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)
    model_config = ConfigDict(extra="ignore")

    email_attachment_id: int | None = Field(default=None, ge=1)
    customer_id: int | None = None
    customer_name: str | None = None
    customer_po: str | None = None
    order_date: date | None = None
    delivery_date: date | None = None
    status: str = "pending_production"
    payment_status: str = "unpaid"
    requisition_strategy: Literal["normal", "wait_previous_batch"] = "normal"
    previous_batch_selections: list["PreviousBatchSelection"] = Field(default_factory=list)
    remark: str | None = None
    items: list[OrderItemCreate] | None = None
    import_integrity_status: str | None = None
    import_integrity_errors: list[str] | None = None
    import_draft: bool = False
    pdf_import_confirmation: PdfImportConfirmation | None = None
    mold_repair_confirmation_token: str | None = Field(default=None, max_length=4000)

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


class PreviousBatchSelection(BaseModel):
    client_line_id: str = Field(min_length=1, max_length=80)
    previous_order_item_id: int = Field(ge=0)


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


def _validated_external_purchase_ratio(
    item: OrderItemCreate,
    *,
    product: Product,
    index: int,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    submitted_order_basis = item.external_packaging_order_quantity_basis
    submitted_purchase_basis = item.external_packaging_purchase_quantity_basis
    if product.supply_mode != "external_purchase":
        if submitted_order_basis is not None or submitted_purchase_basis is not None:
            raise HTTPException(
                status_code=422,
                detail=f"第{index}条不是外购包材，不能填写采购数量换算",
            )
        return None, None, None
    # The common-box profile is the only writable source for the conversion
    # ratio.  Legacy/new clients may still submit the two historical fields,
    # but an order-level value must never override the current master data.
    order_basis = product.external_packaging_default_order_quantity_basis
    purchase_basis = product.external_packaging_default_purchase_quantity_basis
    if order_basis is None or purchase_basis is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"第{index}条外购包材的常用箱尚未设置默认采购比例，"
                "请先在常用箱选择1→1、1→2或自定义比例"
            ),
        )
    ratio = (Decimal(purchase_basis) / Decimal(order_basis)).quantize(
        EXTERNAL_QUANTITY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    if ratio <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"第{index}条外购包材数量换算精度过小，无法形成有效采购数量",
        )
    return Decimal(order_basis), Decimal(purchase_basis), ratio


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
    from app.services.order_import_source import signed_source_lines

    now = datetime.now(timezone.utc)
    states = {**_pdf_preview_safety_states(draft), **(state_overrides or {})}
    claims = {
        "sub": _pdf_preview_actor(user),
        "type": PDF_PREVIEW_SAFETY_TOKEN_TYPE,
        "source_name": str(draft.get("source_name") or ""),
        "source_hash": str(draft.get("file_hash") or ""),
        "source_lines": signed_source_lines(draft),
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


def _decode_pdf_preview_safety_token(
    token: str,
    user: User,
    *,
    observability: dict[str, object] | None = None,
) -> dict:
    required_claims = [
        "sub",
        "type",
        "source_name",
        "source_hash",
        "source_lines",
        "recognition_status",
        "customer_route_status",
        "customer_match_status",
        "integrity_status",
        "matched_customer_id",
        "iat",
        "exp",
    ]
    try:
        claims = jwt.decode(
            token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={"require": required_claims},
        )
    except jwt.ExpiredSignatureError as error:
        # Expiry still blocks the save.  A second signature-verified decode is
        # used only to attach the trusted basename to the failure log.
        try:
            expired_claims = jwt.decode(
                token,
                load_settings().secret_key,
                algorithms=["HS256"],
                options={"require": required_claims, "verify_exp": False},
            )
        except jwt.PyJWTError:
            expired_claims = {}
        if (
            observability is not None
            and expired_claims.get("type") == PDF_PREVIEW_SAFETY_TOKEN_TYPE
            and expired_claims.get("sub") == _pdf_preview_actor(user)
        ):
            observability["pdf_filename"] = _safe_pdf_log_filename(
                expired_claims.get("source_name")
            )
        raise _pdf_preview_token_error("PDF 预览确认已过期，请重新预览") from error
    except jwt.PyJWTError as error:
        raise _pdf_preview_token_error("PDF 预览确认无效，请重新预览") from error
    if (
        claims.get("type") != PDF_PREVIEW_SAFETY_TOKEN_TYPE
        or claims.get("sub") != _pdf_preview_actor(user)
    ):
        raise _pdf_preview_token_error("PDF 预览确认与当前操作员不匹配")
    if observability is not None:
        observability["pdf_filename"] = _safe_pdf_log_filename(
            claims.get("source_name")
        )
    try:
        claims["matched_customer_id"] = int(claims["matched_customer_id"])
    except (TypeError, ValueError) as error:
        raise _pdf_preview_token_error("PDF 预览确认内容无效，请重新预览") from error
    return claims


def _validate_pdf_import_safety(
    payload: OrderCreate,
    user: User,
    *,
    observability: dict[str, object] | None = None,
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

    claims = _decode_pdf_preview_safety_token(
        context.preview_safety_token,
        user,
        observability=observability,
    )
    if claims["integrity_status"] not in {"passed", "manual_confirmed"}:
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


def _product_direct_semi_facts(
    db: Session,
    *,
    product: Product,
    item_payload: OrderItemCreate,
    component_type: str,
) -> tuple[int | None, str | None, int | None, int | None, int | None]:
    """Mirror the order-create layer and crease snapshots before persistence."""

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
    layer_count = (
        selected_material.layer_count
        if selected_material is not None
        else (
            item_payload.layer_count
            if item_payload.layer_count is not None
            else product.layer_count
        )
    )
    if component_type == "base":
        return (
            layer_count,
            product.base_crease_type,
            product.base_crease_left_mm,
            product.base_crease_middle_mm,
            product.base_crease_right_mm,
        )
    return (
        layer_count,
        product.crease_type,
        product.crease_left_mm,
        product.crease_middle_mm,
        product.crease_right_mm,
    )


def _order_item_direct_semi_facts(
    item: OrderItem,
    *,
    component_type: str,
) -> tuple[int | None, str | None, int | None, int | None, int | None]:
    """Read the final immutable order snapshots for the second direct gate."""

    if component_type == "base":
        return (
            item.layer_count,
            item.snapshot_base_crease_type,
            item.snapshot_base_crease_left_mm,
            item.snapshot_base_crease_middle_mm,
            item.snapshot_base_crease_right_mm,
        )
    return (
        item.layer_count,
        item.snapshot_crease_type,
        item.snapshot_crease_left_mm,
        item.snapshot_crease_middle_mm,
        item.snapshot_crease_right_mm,
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
        component_yields: dict[str, int] = {}
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
                current_yield = max(int(detail.stock_yield_per_sheet or 1), 1)
                prior_yield = component_yields.get(entry.component_type)
                if prior_yield is not None and prior_yield != current_yield:
                    raise WarehouseInventoryError(
                        f"第{index}条明细同一组件不能混用不同每张产出的半成品批次",
                        409,
                    )
                component_yields[entry.component_type] = current_yield
                expected_yield = cutting_factor(product.default_cutting_mode)
                expected = _preflight_semi_signature(
                    customer_id=customer_id,
                    product=product,
                    item_payload=item_payload,
                    component_type=entry.component_type,
                    stock_yield_per_sheet=expected_yield,
                )
                scope = ensure_semi_finished_lot_eligibility(
                    db,
                    lot=lot,
                    product_id=product.id,
                    customer_id=customer_id,
                    expected=expected,
                    reviewed=entry.override and SIGNATURE_OVERRIDE_WARNING in entry.warning_acknowledged_codes,
                )
                if rectangular_cut_plan(db, lot, product, expected) is not None:
                    raise WarehouseInventoryError(
                        "该片料需要先在报料中确认分切方案，不能在订单录入阶段直接抵扣",
                        409,
                    )
                if entry.direct_deduction:
                    (
                        layer_count,
                        crease_type,
                        crease_left_mm,
                        crease_middle_mm,
                        crease_right_mm,
                    ) = _product_direct_semi_facts(
                        db,
                        product=product,
                        item_payload=item_payload,
                        component_type=entry.component_type,
                    )
                    if entry.override or not direct_semi_finished_deduction_eligible(
                        db,
                        lot=lot,
                        product_id=product.id,
                        customer_id=customer_id,
                        expected=expected,
                        layer_count=layer_count,
                        crease_type=crease_type,
                        crease_left_mm=crease_left_mm,
                        crease_middle_mm=crease_middle_mm,
                        crease_right_mm=crease_right_mm,
                    ):
                        raise WarehouseInventoryError(
                            "完全匹配资格已变化，请刷新库存候选后重新确认", 409
                        )
                if scope == "general":
                    if entry.direct_deduction:
                        raise WarehouseInventoryError(
                            "跨客户通用半成品不能使用绿色直达抵扣", 409
                        )
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
                if scope == "customer_generic":
                    if entry.recommendation_source != "customer_generic":
                        raise WarehouseInventoryError(
                            "客户通用半成品推荐来源已变化，请刷新", 409
                        )
                    if (
                        CUSTOMER_GENERIC_SEMI_FINISHED_STOCK
                        not in entry.warning_acknowledged_codes
                    ):
                        raise WarehouseInventoryError(
                            "客户通用半成品抵扣必须确认客户库存范围", 409
                        )
                    if not entry.direct_deduction:
                        if not entry.override:
                            raise WarehouseInventoryError(
                                "非完全匹配的客户通用半成品必须人工核对差异", 409
                            )
                        if (
                            SIGNATURE_OVERRIDE_WARNING
                            not in entry.warning_acknowledged_codes
                        ):
                            raise WarehouseInventoryError(
                                "人工核对客户通用半成品必须确认签名差异警告", 409
                            )
                    continue
                if entry.recommendation_source == "customer_generic":
                    raise WarehouseInventoryError(
                        "专用半成品库存不能伪造为客户通用库存推荐", 409
                    )
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
    return box_type_code(product.box_style) == "a3_set"


def _order_snapshot_box_configuration(product: Product) -> dict[str, object]:
    """Normalize recognized types while preserving unknown historical values."""
    if get_box_type_rule(product.box_style) is None:
        splice_mode = (product.splice_mode or "single").strip().lower()
        return {
            "recognized": False,
            "code": None,
            "box_style": (product.box_style or "").strip() or None,
            "splice_mode": splice_mode,
            "pieces_per_box": (
                product.pieces_per_box
                if product.pieces_per_box is not None
                else (2 if splice_mode == "double" else 1)
            ),
            "flap_mm": product.flap_mm,
            "default_cutting_mode": (
                product.default_cutting_mode or "一开一"
            ),
        }
    return normalize_box_configuration(
        box_style=product.box_style,
        splice_mode=product.splice_mode,
        pieces_per_box=product.pieces_per_box,
        flap_mm=product.flap_mm,
        default_cutting_mode=product.default_cutting_mode,
        crease_type=product.crease_type,
    )


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
            stock_yield_per_sheet = cutting_output_factor(item.special_process)
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
            if entry.direct_deduction:
                (
                    layer_count,
                    crease_type,
                    crease_left_mm,
                    crease_middle_mm,
                    crease_right_mm,
                ) = _order_item_direct_semi_facts(
                    item,
                    component_type=entry.component_type,
                )
                if entry.override or not direct_semi_finished_deduction_eligible(
                    db,
                    lot=lot,
                    product_id=item.product_id,
                    customer_id=order.customer_id,
                    expected=requirement_signature(requirement),
                    layer_count=layer_count,
                    crease_type=crease_type,
                    crease_left_mm=crease_left_mm,
                    crease_middle_mm=crease_middle_mm,
                    crease_right_mm=crease_right_mm,
                ):
                    raise WarehouseInventoryError(
                        "完全匹配资格在保存前已变化，整单保存已取消，请刷新后重试",
                        409,
                    )
            if entry.recommendation_source == "customer_generic":
                if candidate is None or candidate.source != "customer_generic":
                    raise WarehouseInventoryError(
                        "客户通用半成品推荐资格已变化，请刷新", 409
                    )
                if (
                    CUSTOMER_GENERIC_SEMI_FINISHED_STOCK
                    not in entry.warning_acknowledged_codes
                ):
                    raise WarehouseInventoryError(
                        "客户通用半成品抵扣必须确认客户库存范围", 409
                    )
                if not entry.direct_deduction:
                    if not entry.override or (
                        SIGNATURE_OVERRIDE_WARNING
                        not in entry.warning_acknowledged_codes
                    ):
                        raise WarehouseInventoryError(
                            "非完全匹配的客户通用半成品必须人工核对差异", 409
                        )
            elif entry.recommendation_source == "general_signature":
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
    mold_repair_confirmation_token: str | None = Field(default=None, max_length=4000)


class MoldRepairPreviewRequest(BaseModel):
    product_ids: list[int] = Field(default_factory=list, max_length=500)
    order_ids: list[int] = Field(default_factory=list, max_length=500)
    order_item_ids: list[int] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def require_target(self) -> "MoldRepairPreviewRequest":
        if not self.product_ids and not self.order_ids and not self.order_item_ids:
            raise ValueError("至少提供一个订单、订单明细或常用箱")
        if any(value <= 0 for value in (*self.product_ids, *self.order_ids, *self.order_item_ids)):
            raise ValueError("订单维修提醒对象无效")
        return self


def _order_product_ids(
    db: Session,
    *,
    order_ids: list[int] | None = None,
    order_item_ids: list[int] | None = None,
) -> list[int]:
    conditions = []
    if order_ids:
        conditions.append(OrderItem.order_id.in_(set(order_ids)))
    if order_item_ids:
        conditions.append(OrderItem.id.in_(set(order_item_ids)))
    if not conditions:
        return []
    return sorted(
        {
            int(value)
            for value in db.scalars(
                select(OrderItem.product_id).where(
                    or_(*conditions),
                    OrderItem.product_id.is_not(None),
                )
            ).all()
            if value is not None
        }
    )


def _require_product_scope_for_warning(
    db: Session,
    *,
    product_ids: list[int],
    user: User,
) -> None:
    customer_ids = set(
        db.scalars(select(Product.customer_id).where(Product.id.in_(product_ids))).all()
    ) if product_ids else set()
    for customer_id in customer_ids:
        require_customer_access(customer_id, current_user=user, db=db)


@router.post("/mold-repair-preview")
def preview_order_mold_repairs(
    payload: MoldRepairPreviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    product_ids = sorted(
        set(payload.product_ids)
        | set(
            _order_product_ids(
                db,
                order_ids=payload.order_ids,
                order_item_ids=payload.order_item_ids,
            )
        )
    )
    _require_product_scope_for_warning(db, product_ids=product_ids, user=user)
    warnings = repair_warnings_for_products(db, product_ids)
    return {
        "required": bool(warnings),
        "warnings": warnings,
        "confirmation_token": issue_order_mold_repair_confirmation(warnings=warnings, user=user),
    }


class DraftRematchRequest(BaseModel):
    draft: dict
    customer_id: int
    preview_safety_token: str = Field(min_length=1, max_length=4000)


class ManualDraftRematchRequest(DraftRematchRequest):
    manual_complete: Literal[True]


class CostPreviewRequest(BaseModel):
    product_id: int
    material_id: int | None = None
    flute_type: str | None = None   # v0.19.2-B: 传楞型以计入加价


class OrderStatusRequest(BaseModel):
    status: str
    remark: str | None = Field(default=None, max_length=500)


class WorkflowRollbackRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


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


def _order_item_material_margin(
    *,
    subtotal: Decimal,
    material_total: object,
) -> dict[str, str | None]:
    """Project material-only gross profit without treating it as full profit."""

    total = Decimal(str(material_total))
    gross = subtotal - total
    margin = gross / subtotal if subtotal > 0 else None
    return {
        "material_sales_amount": str(subtotal.quantize(MONEY_QUANTUM)),
        "material_gross_profit": str(gross.quantize(MONEY_QUANTUM)),
        "material_gross_margin": (
            str(margin.quantize(Decimal("0.0001"))) if margin is not None else None
        ),
        "material_gross_scope_label": "材料毛利（未扣加工、人工、运输和损耗）",
    }


def _product_combination_mode(product: Product) -> str:
    """Read the explicit mode while keeping old composite rows compatible."""
    if not product.is_composite:
        return "standalone"
    mode = getattr(product, "combination_mode", None)
    return str(mode) if mode else "parent_priced_set"


def _product_composite_fulfillment_mode(product: Product) -> str:
    if not product.is_composite:
        return "component_delivery"
    mode = str(
        getattr(product, "composite_fulfillment_mode", "component_delivery")
        or "component_delivery"
    )
    return mode if mode in {"parent_delivery", "component_delivery"} else "component_delivery"


def _validated_combination_provenance(
    db: Session,
    *,
    customer: Customer,
    item_payload: OrderItemCreate,
    product: Product,
    item_index: int,
) -> dict[str, object]:
    """Return only server-verified combination facts for one new order item."""
    requested_fields = (
        item_payload.combination_mode_snapshot,
        item_payload.combination_role,
        item_payload.combination_group_key,
        item_payload.combination_parent_product_id,
        item_payload.combination_parent_name_snapshot,
        item_payload.combination_set_quantity_snapshot,
        item_payload.combination_quantity_per_set_snapshot,
    )
    product_mode = _product_combination_mode(product)
    # A physical subassembly can be priced as a child of another combination.
    # Its upstream provenance is validated below; its own graph is frozen by
    # the normal graph-order writer. Legacy composites lack that graph contract.
    nested_physical_child = False
    if item_payload.combination_role == "priced_component" and product_mode == "parent_priced_set":
        from app.models.multilevel_bom import ProductBomProfile
        profile = db.get(ProductBomProfile, product.id)
        nested_physical_child = profile is not None and profile.source in {
            "assembled", "manufactured", "purchased",
        }

    if product_mode == "parent_priced_set" and not nested_physical_child:
        if any(value is not None for value in requested_fields):
            raise HTTPException(
                status_code=400,
                detail=f"第{item_index}条明细组合父件来源由系统生成，不能由客户端填写",
            )
        fulfillment_mode = (
            item_payload.composite_fulfillment_mode_snapshot
            or _product_composite_fulfillment_mode(product)
        )
        parent_label_enabled = bool(
            fulfillment_mode == "parent_delivery"
            and product.production_label_enabled
        )
        return {
            "combination_mode_snapshot": "parent_priced_set",
            "composite_fulfillment_mode_snapshot": fulfillment_mode,
            "parent_production_label_enabled_snapshot": parent_label_enabled,
            "parent_production_label_units_per_label_snapshot": (
                int(product.production_label_units_per_label)
                if parent_label_enabled
                and product.production_label_units_per_label is not None
                else None
            ),
            "parent_production_label_template_version_snapshot": (
                CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
            ),
            "parent_production_label_product_version_snapshot": int(product.version),
            "combination_role": "set_parent",
            "combination_group_key": None,
            "combination_parent_product_id": None,
            "combination_parent_name_snapshot": None,
            "combination_set_quantity_snapshot": None,
            "combination_quantity_per_set_snapshot": None,
        }

    physical_priced_parent = False
    if product_mode == "component_priced" and item_payload.combination_parent_product_id == product.id:
        from app.models.multilevel_bom import ProductBomProfile
        profile = db.get(ProductBomProfile, product.id)
        physical_priced_parent = bool(profile and profile.source in {"manufactured", "purchased"}
            and not product.is_virtual_composite_parent and item_payload.combination_role == "priced_component")
    if product_mode == "component_priced" and not physical_priced_parent:
        raise HTTPException(
            status_code=400,
            detail=(
                f"第{item_index}条明细是“组件分别计价”组合父件，不能直接保存为订单明细；"
                "请先展开并填写各组件的数量和单价"
            ),
        )

    is_priced_component = item_payload.combination_role == "priced_component"
    if not is_priced_component:
        if any(value is not None for value in requested_fields) or (
            item_payload.composite_fulfillment_mode_snapshot is not None
        ):
            raise HTTPException(
                status_code=400,
                detail=f"第{item_index}条明细不是有效的组合组件来源",
            )
        return {
            "combination_mode_snapshot": None,
            "composite_fulfillment_mode_snapshot": None,
            "parent_production_label_enabled_snapshot": None,
            "parent_production_label_units_per_label_snapshot": None,
            "parent_production_label_template_version_snapshot": None,
            "parent_production_label_product_version_snapshot": None,
            "combination_role": "standalone",
            "combination_group_key": None,
            "combination_parent_product_id": None,
            "combination_parent_name_snapshot": None,
            "combination_set_quantity_snapshot": None,
            "combination_quantity_per_set_snapshot": None,
        }

    if (
        item_payload.combination_mode_snapshot != "component_priced"
        or item_payload.composite_fulfillment_mode_snapshot
        not in {None, "component_delivery"}
        or not (item_payload.combination_group_key or "").strip()
        or item_payload.combination_parent_product_id is None
        or not (item_payload.combination_parent_name_snapshot or "").strip()
        or item_payload.combination_set_quantity_snapshot is None
        or item_payload.combination_quantity_per_set_snapshot is None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"第{item_index}条分别计价组件缺少组合来源信息",
        )

    parent = db.get(Product, item_payload.combination_parent_product_id)
    if (
        parent is None
        or parent.deleted_at is not None
        or not parent.is_active
        or parent.customer_id != customer.id
        or _product_combination_mode(parent) != "component_priced"
    ):
        raise HTTPException(
            status_code=400,
            detail=f"第{item_index}条分别计价组件的组合父件无效或不属于当前客户",
        )
    if (item_payload.combination_parent_name_snapshot or "").strip() != parent.product_name:
        raise HTTPException(
            status_code=400,
            detail=f"第{item_index}条分别计价组件的组合父件名称不一致",
        )
    relation = db.scalar(
        select(ProductBomComponent).where(
            ProductBomComponent.parent_product_id == parent.id,
            ProductBomComponent.component_product_id == product.id,
        )
    )
    if relation is None and not physical_priced_parent:
        raise HTTPException(
            status_code=400,
            detail=f"第{item_index}条产品不是该组合父件的组件",
        )
    expected_per_set = 1 if physical_priced_parent else int(Decimal(str(relation.quantity_per_set)))
    if item_payload.combination_quantity_per_set_snapshot != expected_per_set:
        raise HTTPException(
            status_code=400,
            detail=f"第{item_index}条组件每套数量与组合 BOM 不一致",
        )
    return {
        "combination_mode_snapshot": "component_priced",
        "composite_fulfillment_mode_snapshot": "component_delivery",
        "parent_production_label_enabled_snapshot": None,
        "parent_production_label_units_per_label_snapshot": None,
        "parent_production_label_template_version_snapshot": None,
        "parent_production_label_product_version_snapshot": None,
        "combination_role": "priced_component",
        "combination_group_key": item_payload.combination_group_key.strip(),
        "combination_parent_product_id": parent.id,
        "combination_parent_name_snapshot": parent.product_name,
        "combination_set_quantity_snapshot": item_payload.combination_set_quantity_snapshot,
        "combination_quantity_per_set_snapshot": expected_per_set,
    }


def _validate_combination_group_consistency(
    provenances: dict[int, dict[str, object]],
) -> None:
    """Keep one client group key bound to one immutable parent and set count."""
    groups: dict[str, tuple[int, str, int]] = {}
    for item_index, provenance in provenances.items():
        if provenance.get("combination_role") != "priced_component":
            continue
        key = str(provenance["combination_group_key"])
        signature = (
            int(provenance["combination_parent_product_id"]),
            str(provenance["combination_parent_name_snapshot"]),
            int(provenance["combination_set_quantity_snapshot"]),
        )
        existing = groups.setdefault(key, signature)
        if existing != signature:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第{item_index}条明细与同一组合分组的父件或套数不一致；"
                    "请重新选择组合父件后再保存"
                ),
            )


def _display_material(value: str | None) -> str | None:
    text = value
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
                DeliveryItem.is_current.is_(True),
                Delivery.status == "dispatched",
            )
            .group_by(DeliveryItem.order_item_id)
        ).all()
        if completion_date is not None
    }


def _build_full_order_response_context(
    db: Session,
    orders: list[Order],
    user: User,
    *,
    business_projections: dict[int, dict] | None = None,
) -> dict:
    """Batch every read-only dependency shared by full order serializers."""

    items = [item for order in orders for item in order.items]
    item_ids = [int(item.id) for item in items]
    completion_dates = _completion_dates_by_item(db, item_ids) if item_ids else {}
    bom_components_by_item_id = (
        get_order_item_bom_components_by_item_ids(db, item_ids) if item_ids else {}
    )
    external_components_by_item_id = (
        get_order_item_external_components_by_item_ids(db, items) if items else {}
    )
    orders_with_external_requirements = [
        int(order.id)
        for order in orders
        if any(
            external_components_by_item_id.get(int(item.id))
            for item in order.items
        )
    ]
    external_purchase_summaries_by_order_id = (
        get_external_purchase_summaries_by_order_ids(
            db, orders_with_external_requirements
        )
        if orders_with_external_requirements
        else {}
    )
    finished_reservations_by_item_id = (
        active_finished_reservations_by_item_ids(db, item_ids) if item_ids else {}
    )
    active_holds_by_item_id = (
        {
            int(hold.order_item_id): hold
            for hold in db.scalars(
                select(RequisitionHold).where(
                    RequisitionHold.order_item_id.in_(item_ids),
                    RequisitionHold.status == "active",
                )
            ).all()
            if hold.order_item_id is not None
        }
        if item_ids
        else {}
    )
    may_view_cost = has_permission(user, "cost.view")
    frozen_material_costs_by_item_id = (
        get_latest_order_item_material_cost_snapshots_by_items(db, items)
        if may_view_cost and items
        else {}
    )
    frozen_estimated_costs_by_item_id = (
        get_latest_order_item_estimated_cost_snapshots_by_items(db, items)
        if may_view_cost and items
        else {}
    )
    current_estimate_items = [
        item
        for item in items
        if int(item.id) not in frozen_material_costs_by_item_id
    ]
    material_cost_context = (
        build_material_cost_estimate_context(
            db,
            current_estimate_items,
            bom_components_by_item_id=bom_components_by_item_id,
        )
        if may_view_cost and current_estimate_items
        else None
    )
    resolved_business_projections = business_projections
    if resolved_business_projections is None:
        resolved_business_projections = (
            build_order_business_statuses(
                db,
                orders,
                include_finance=has_permission(user, "finance.view"),
            )
            if orders
            else {}
        )
    return {
        "completion_dates": completion_dates,
        "bom_components_by_item_id": bom_components_by_item_id,
        "external_components_by_item_id": external_components_by_item_id,
        "business_projections": resolved_business_projections,
        "active_holds_by_item_id": active_holds_by_item_id,
        "finished_reservations_by_item_id": finished_reservations_by_item_id,
        "external_purchase_summaries_by_order_id": (
            external_purchase_summaries_by_order_id
        ),
        "frozen_material_costs_by_item_id": frozen_material_costs_by_item_id,
        "frozen_estimated_costs_by_item_id": frozen_estimated_costs_by_item_id,
        "material_cost_context": material_cost_context,
    }


def _full_order_response_kwargs(context: dict, order_id: int) -> dict:
    return {
        "completion_dates": context["completion_dates"],
        "bom_components_by_item_id": context["bom_components_by_item_id"],
        "external_components_by_item_id": context[
            "external_components_by_item_id"
        ],
        "business_projection": context["business_projections"].get(order_id),
        "active_holds_by_item_id": context["active_holds_by_item_id"],
        "finished_reservations_by_item_id": context[
            "finished_reservations_by_item_id"
        ],
        "external_purchase_summaries_by_order_id": context[
            "external_purchase_summaries_by_order_id"
        ],
        "frozen_material_costs_by_item_id": context[
            "frozen_material_costs_by_item_id"
        ],
        "frozen_estimated_costs_by_item_id": context[
            "frozen_estimated_costs_by_item_id"
        ],
        "material_cost_context": context["material_cost_context"],
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
    external_components_by_item_id: dict[int, list[dict]] | None = None,
    business_projection: dict | None = None,
    active_holds_by_item_id: dict[int, RequisitionHold] | None = None,
    finished_reservations_by_item_id: dict[int, int] | None = None,
    external_purchase_summaries_by_order_id: dict[int, dict] | None = None,
    frozen_material_costs_by_item_id: dict[
        int, SalesOrderItemMaterialCostSnapshot
    ]
    | None = None,
    frozen_estimated_costs_by_item_id: dict[
        int, SalesOrderItemEstimatedCostSnapshot
    ]
    | None = None,
    material_cost_context: MaterialCostEstimateContext | None = None,
) -> dict:
    item_ids = [item.id for item in order.items]
    cancelled_external_ids = (fully_cancelled_external_item_ids(db, [
        item.id for item in order.items if item.requisition_status == "外购包材已采购"
        and item.material_status == "pending"
    ]) if db is not None else set())
    reservation_map = finished_reservations_by_item_id
    if reservation_map is None:
        reservation_map = (
            active_finished_reservations_by_item_ids(
                db, item_ids
            )
            if db is not None
            else {}
        )
    active_hold_map = active_holds_by_item_id
    if active_hold_map is None:
        active_hold_map = (
            {
                int(hold.order_item_id): hold
                for hold in db.scalars(
                    select(RequisitionHold).where(
                        RequisitionHold.order_item_id.in_(item_ids),
                        RequisitionHold.status == "active",
                    )
                ).all()
                if hold.order_item_id is not None
            }
            if db is not None and item_ids
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
    if external_components_by_item_id is None:
        external_components_by_item_id = (
            get_order_item_external_components_by_item_ids(db, list(order.items))
            if db is not None
            else {}
        )
    has_external_requirements = any(
        external_components_by_item_id.get(item.id) for item in order.items
    )
    if not has_external_requirements:
        external_purchase_summary = None
    elif external_purchase_summaries_by_order_id is not None:
        external_purchase_summary = external_purchase_summaries_by_order_id.get(
            int(order.id),
            {"status": "pending", "purchase_numbers": []},
        )
    else:
        external_purchase_summary = (
            get_external_purchase_summary(db, int(order.id))
            if db is not None
            else None
        )
    if business_projection is None and db is not None:
        business_projection = build_order_business_statuses(
            db,
            [order],
            include_finance=has_permission(user, "finance.view"),
        ).get(
            int(order.id),
        )
    business_projection = business_projection or {}
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
        "business_status": business_projection.get("business_status", order.status),
        "business_status_label": business_projection.get(
            "business_status_label"
        ),
        "business_status_evidence": business_projection.get(
            "business_status_evidence"
        ),
        "business_delivery_progress": business_projection.get(
            "business_delivery_progress"
        ),
        "business_item_status_counts": business_projection.get(
            "business_item_status_counts", {}
        ),
        "payment_status": order.payment_status,
        "total_amount": order.total_amount,
        "remark": order.remark,
        "external_packaging_purchase_summary": external_purchase_summary,
        "items": [],
    }
    may_view_cost = has_permission(user, "cost.view")
    frozen_cost_by_item_id = frozen_material_costs_by_item_id
    if frozen_cost_by_item_id is None:
        frozen_cost_by_item_id = (
            get_latest_order_item_material_cost_snapshots_by_items(
                db, list(order.items)
            )
            if db is not None and may_view_cost
            else {}
        )
    frozen_estimated_cost_by_item_id = frozen_estimated_costs_by_item_id
    if frozen_estimated_cost_by_item_id is None:
        frozen_estimated_cost_by_item_id = (
            get_latest_order_item_estimated_cost_snapshots_by_items(
                db, list(order.items)
            )
            if db is not None and may_view_cost
            else {}
        )
    for item in order.items:
        item_business_projection = business_projection.get("items", {}).get(
            int(item.id), {}
        )
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
        item_bom_components = bom_components_by_item_id.get(item.id, [])
        cost_reference = {}
        if db is not None and may_view_cost:
            frozen_cost = frozen_cost_by_item_id.get(item.id)
            cost_reference = (
                serialize_order_item_material_cost_snapshot(frozen_cost)
                if frozen_cost is not None
                else mark_current_estimate_as_non_historical(
                    estimate_order_item_material_cost(
                        db,
                        item,
                        bom_components=item_bom_components,
                        context=material_cost_context,
                    )
                )
            )
            frozen_estimated_cost = frozen_estimated_cost_by_item_id.get(item.id)
            if frozen_estimated_cost is not None:
                cost_reference.update(
                    serialize_order_item_estimated_cost_snapshot(
                        frozen_estimated_cost,
                        sale_amount=item.subtotal,
                    )
                )
            else:
                cost_reference.update(
                    {
                        "estimated_total_cost_status": "not_frozen",
                        "estimated_total_cost_status_label": "预计总成本待冻结",
                        "estimated_total_cost_scope_label": "预计成本，非实际成本",
                        "cost_status": "pending",
                        "estimated_cost": None,
                    }
                )
        item_data = {
                "id": item.id,
                "product_id": item.product_id,
                "item_order_number": item.item_order_number,
                "item_sequence": item.item_sequence,
                "quantity": item.quantity,
                "ordered_quantity": item.quantity,
                "combination_mode_snapshot": item.combination_mode_snapshot,
                "composite_fulfillment_mode_snapshot": getattr(
                    item, "composite_fulfillment_mode_snapshot", None
                ),
                "parent_production_label_enabled_snapshot": getattr(
                    item, "parent_production_label_enabled_snapshot", None
                ),
                "parent_production_label_units_per_label_snapshot": getattr(
                    item, "parent_production_label_units_per_label_snapshot", None
                ),
                "is_virtual_composite_parent_snapshot": bool(
                    getattr(item, "is_virtual_composite_parent_snapshot", False)
                ),
                "combination_role": item.combination_role,
                "combination_group_key": item.combination_group_key,
                "combination_parent_product_id": item.combination_parent_product_id,
                "combination_parent_name_snapshot": item.combination_parent_name_snapshot,
                "combination_set_quantity_snapshot": item.combination_set_quantity_snapshot,
                "combination_quantity_per_set_snapshot": item.combination_quantity_per_set_snapshot,
                "bom_components": item_bom_components,
                "bom_production_revision": max((row.get("production_revision", 0) for row in item_bom_components), default=0),
                "external_packaging_requirements": external_components_by_item_id.get(
                    item.id, []
                ),
                "delivered_quantity": item.delivered_quantity,
                "remaining_quantity": max(
                    int(item.quantity or 0) - int(item.delivered_quantity or 0),
                    0,
                ),
                "business_status": item_business_projection.get(
                    "business_status"
                ),
                "business_status_label": item_business_projection.get(
                    "business_status_label"
                ),
                "business_status_evidence": item_business_projection.get(
                    "business_status_evidence"
                ),
                "business_delivered_quantity": item_business_projection.get(
                    "business_delivered_quantity",
                    int(item.delivered_quantity or 0),
                ),
                "business_remaining_quantity": item_business_projection.get(
                    "business_remaining_quantity",
                    max(
                        int(item.quantity or 0)
                        - int(item.delivered_quantity or 0),
                        0,
                    ),
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
                "price_tax_mode_snapshot": item.price_tax_mode_snapshot,
                "tax_rate_snapshot": item.tax_rate_snapshot,
                "material_status": item.material_status,
                "material_received_at": (
                    utc_naive_to_api(item.material_received_at)
                    if item.material_received_at
                    else None
                ),
                "snapshot_product_code": item.snapshot_product_code,
                "snapshot_product_name": item.snapshot_product_name,
                "snapshot_spec": resolved_product_specification(
                    item.snapshot_spec,
                    item.product,
                ),
                "snapshot_material": item.snapshot_material,
                "snapshot_original_material_code": item.snapshot_original_material_code,
                "snapshot_customer_model": item.snapshot_customer_model,  # v0.19.1
                "snapshot_production_notes": item.snapshot_production_notes,  # v0.19.2-A
                "supply_mode_snapshot": item.supply_mode_snapshot,
                "external_packaging_category_code_snapshot": item.external_packaging_category_code_snapshot,
                "external_packaging_specification_summary_snapshot": item.external_packaging_specification_summary_snapshot,
                "external_packaging_purchase_unit_snapshot": item.external_packaging_purchase_unit_snapshot,
                "external_packaging_order_quantity_basis_snapshot": item.external_packaging_order_quantity_basis_snapshot,
                "external_packaging_purchase_quantity_basis_snapshot": item.external_packaging_purchase_quantity_basis_snapshot,
                "external_packaging_quantity_per_finished_unit_snapshot": item.external_packaging_quantity_per_finished_unit_snapshot,
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
                "mold_location_display": (
                    describe_mold_location(mold_tool.rack_location)["prompt"]
                    if mold_tool
                    else None
                ),
                "inventory_deducted_qty": item.inventory_deducted_qty,
                "finished_inventory_reserved_qty": finished_reserved_quantity,
                "production_required_qty": production_required_quantity,
                "fully_covered_by_finished_inventory": (
                    production_required_quantity == 0
                ),
                "requisition_qty": item.requisition_qty,
                "requisition_status": "未报料" if item.id in cancelled_external_ids else item.requisition_status,
                "requisition_hold": (
                    {
                        "id": active_hold_map[item.id].id,
                        "status": "active",
                        "label": "等候中",
                        "release_mode": active_hold_map[item.id].release_mode,
                        "expected_requisition_date": (
                            active_hold_map[item.id].expected_requisition_date
                        ),
                        "version": active_hold_map[item.id].version,
                    }
                    if item.id in active_hold_map
                    else None
                ),
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
        if (
            may_view_cost
            and _can_view_order_sales_amount(user)
            and item_data.get("estimated_material_total_cost") is not None
        ):
            item_data.update(
                _order_item_material_margin(
                    subtotal=Decimal(str(item.subtotal)),
                    material_total=item_data["estimated_material_total_cost"],
                )
            )
        elif may_view_cost:
            item_data.update(
                {
                    "material_sales_amount": None,
                    "material_gross_profit": None,
                    "material_gross_margin": None,
                    "material_gross_scope_label": "材料毛利（未扣加工、人工、运输和损耗）",
                }
            )
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
    if not _can_view_order_sales_amount(user):
        data.pop("total_amount", None)
        data.pop("payment_status", None)
        for item in data["items"]:
            item.pop("unit_price", None)
            item.pop("subtotal", None)
            item.pop("sale_amount", None)
    return data


def _order_list_summary_response(
    order: Order,
    user: User,
    *,
    customer_name: str | None = None,
    display_registry=None,
    business_projection: dict | None = None,
) -> dict:
    """Serialize only fields rendered before an order group is expanded."""

    business_projection = business_projection or {}
    item_projections = business_projection.get("items", {})
    total_quantity = sum(int(item.quantity or 0) for item in order.items)
    business_remaining_quantity = sum(
        int(
            item_projections.get(int(item.id), {}).get(
                "business_remaining_quantity",
                max(
                    int(item.quantity or 0) - int(item.delivered_quantity or 0),
                    0,
                ),
            )
            or 0
        )
        for item in order.items
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
        "business_status": business_projection.get("business_status", order.status),
        "business_status_label": business_projection.get("business_status_label"),
        "business_delivery_progress": business_projection.get(
            "business_delivery_progress"
        ),
        "total_amount": order.total_amount,
        "item_count": len(order.items),
        "total_quantity": total_quantity,
        "all_material_received": all(
            item.material_status == "received" for item in order.items
        ),
        "business_remaining_quantity": business_remaining_quantity,
    }
    if not _can_view_order_sales_amount(user):
        data.pop("total_amount", None)
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
    customer_ids: list[int] | None = Query(default=None),
    keyword: str | None = None,
    order_number: str | None = None,
    customer_po: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    specification: str | None = None,
    customer_name: str | None = None,
    order_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    order_date_from: date | None = None,
    order_date_to: date | None = None,
    delivery_date_from: date | None = None,
    delivery_date_to: date | None = None,
    status_filter: list[str] | None = Query(default=None, alias="status"),
    scope: Literal["active", "completed", "cancelled", "all"] | None = Query(default=None),
    stage: list[str] | None = Query(default=None),
    sort_by: Literal["customer_name", "order_date", "delivery_date"] | None = None,
    sort_direction: Literal["asc", "desc"] = "desc",
    detail_level: Literal["full", "summary"] = "full",
    include_unfinished_total: bool = Depends(_include_order_list_unfinished_total),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    display_registry = build_display_registry(db)
    search_keyword = keyword.strip() if keyword and keyword.strip() else None
    status_values = tuple(
        dict.fromkeys(value.strip() for value in (status_filter or []) if value.strip())
    )
    stage_values = tuple(
        dict.fromkeys(value.strip() for value in (stage or []) if value.strip())
    )
    requested_customer_ids = set(customer_ids or [])
    if customer_id is not None:
        requested_customer_ids.add(customer_id)
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

    if requested_customer_ids:
        for requested_customer_id in requested_customer_ids:
            require_customer_access(requested_customer_id, current_user=user, db=db)
        ids_query = ids_query.where(Order.customer_id.in_(requested_customer_ids))

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
    if order_date_from is not None:
        ids_query = ids_query.where(Order.order_date >= order_date_from)
    if order_date_to is not None:
        ids_query = ids_query.where(Order.order_date <= order_date_to)
    if delivery_date_from is not None:
        ids_query = ids_query.where(Order.delivery_date >= delivery_date_from)
    if delivery_date_to is not None:
        ids_query = ids_query.where(Order.delivery_date <= delivery_date_to)

    raw_status_filters = {
        status_value
        for status_value in status_values
        if status_value != "business"
        and status_value not in DERIVED_BUSINESS_STATUSES
        and status_value not in _DERIVED_STATUS_FILTER_GROUPS
    }
    requested_stage_values = stage_values or tuple(
        value for value in status_values if value != "business"
    )
    derived_status_filter: set[str] = set()
    for stage_value in requested_stage_values:
        if stage_value in DERIVED_BUSINESS_STATUSES or stage_value == "pending_confirmation":
            derived_status_filter.add(stage_value)
        elif stage_value in _DERIVED_STATUS_FILTER_GROUPS:
            derived_status_filter.update(_DERIVED_STATUS_FILTER_GROUPS[stage_value])

    if scope is None:
        if status_values == ("completed",):
            # Keep the historical ``status=completed`` query compatible while
            # the new UI uses the explicit ``scope=completed`` contract.
            resolved_scope = "completed"
        elif raw_status_filters & set(_BUSINESS_EXCLUDED_STATUSES):
            resolved_scope = "cancelled"
        else:
            resolved_scope = "active"
    else:
        resolved_scope = scope
    if resolved_scope in {"active", "completed"}:
        ids_query = ids_query.where(
            Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
        )
    elif resolved_scope == "cancelled":
        ids_query = ids_query.where(Order.status.in_(_BUSINESS_EXCLUDED_STATUSES))
    if raw_status_filters:
        ids_query = ids_query.where(Order.status.in_(raw_status_filters))

    if search_keyword:
        trimmed = search_keyword
        customer_identity_filter = customer_identity_search_clause(trimmed)
        assert customer_identity_filter is not None
        ids_query = ids_query.outerjoin(
            OrderItem, OrderItem.order_id == Order.id
        ).outerjoin(
            Product, Product.id == OrderItem.product_id
        ).where(
            or_(
                Order.order_number.ilike(f"%{trimmed}%"),
                Order.customer_po.ilike(f"%{trimmed}%"),
                customer_identity_filter,
                OrderItem.item_order_number.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_product_code.ilike(f"%{trimmed}%"),
                Product.product_code.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_product_name.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_spec.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_material.ilike(f"%{trimmed}%"),
            )
        )
        joined_items = True

    if order_number and order_number.strip():
        trimmed = order_number.strip()
        if not joined_items:
            ids_query = ids_query.outerjoin(
                OrderItem, OrderItem.order_id == Order.id
            )
        ids_query = ids_query.where(
            or_(
                Order.order_number == trimmed,
                OrderItem.item_order_number == trimmed,
            )
        )

    def _join_items_for_filter() -> None:
        nonlocal ids_query, joined_items
        if not joined_items:
            ids_query = ids_query.outerjoin(
                OrderItem, OrderItem.order_id == Order.id
            ).outerjoin(Product, Product.id == OrderItem.product_id)
            joined_items = True

    if customer_po and customer_po.strip():
        ids_query = ids_query.where(Order.customer_po.ilike(f"%{customer_po.strip()}%"))
    if product_code and product_code.strip():
        _join_items_for_filter()
        trimmed = product_code.strip()
        ids_query = ids_query.where(
            or_(
                OrderItem.snapshot_product_code.ilike(f"%{trimmed}%"),
                Product.product_code.ilike(f"%{trimmed}%"),
                Product.customer_material_code.ilike(f"%{trimmed}%"),
            )
        )
    if product_name and product_name.strip():
        _join_items_for_filter()
        ids_query = ids_query.where(
            OrderItem.snapshot_product_name.ilike(f"%{product_name.strip()}%")
        )
    if specification and specification.strip():
        _join_items_for_filter()
        ids_query = ids_query.where(
            OrderItem.snapshot_spec.ilike(f"%{specification.strip()}%")
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
    # Business views sort by creation time so a freshly saved
    # order surfaces at the top even when its order_date is back-dated to the
    # source document date (e.g. PDF imports), instead of being buried below
    # newer-dated rows where users assume it "disappeared".
    else:
        ids_query = ids_query.order_by(
            Order.created_at.desc(),
            Order.id.desc(),
        )
    candidate_ids: list[int] = []
    candidate_orders: list[Order] = []
    candidate_order_map: dict[int, Order] = {}
    candidate_projection: dict[int, dict] = {}
    badge_unfinished_ids: list[int] | None = None
    badge_unfinished_projections: dict[int, dict] | None = None
    badge_unfinished_order_map: dict[int, Order] = {}
    badge_snapshot_cache_hit = False
    needs_business_projection = bool(derived_status_filter) or resolved_scope in {
        "active",
        "completed",
    }
    if needs_business_projection:
        candidate_ids = list(db.scalars(ids_query).all())
        # Both filtered and unfiltered active lists use the same authoritative
        # scope/finance snapshot. Filtering must not decide cache eligibility.
        if (
            include_unfinished_total
            and resolved_scope == "active"
            and not derived_status_filter
            and not raw_status_filters
        ):
            badge_snapshot, badge_unfinished_order_map, badge_snapshot_cache_hit = (
                active_order_status_badge_snapshot(
                    db,
                    scoped_customer_ids=(
                        scoped_customer_ids if is_customer_scope_restricted else None
                    ),
                    include_finance=has_permission(user, "finance.view"),
                )
            )
            badge_unfinished_ids = list(badge_snapshot.order_ids)
            badge_unfinished_projections = badge_snapshot.projections
            candidate_orders = (
                [
                    badge_unfinished_order_map[order_id]
                    for order_id in candidate_ids
                    if order_id in badge_unfinished_order_map
                ]
                if not badge_snapshot_cache_hit
                else []
            )
            missing_candidate_ids = [
                order_id
                for order_id in candidate_ids
                if order_id not in badge_unfinished_projections
            ]
            if missing_candidate_ids:
                candidate_orders.extend(
                    db.scalars(
                        select(Order)
                        .options(*order_status_projection_load_options(include_order_summary=True))
                        .where(Order.id.in_(missing_candidate_ids))
                    ).all()
                )
        elif candidate_ids:
            candidate_orders = list(
                db.scalars(
                    select(Order)
                    .options(*order_status_projection_load_options(include_order_summary=True))
                    .where(Order.id.in_(candidate_ids))
                ).all()
            )
        candidate_projection = (
            {
                order_id: badge_unfinished_projections[order_id]
                for order_id in candidate_ids
                if badge_unfinished_projections is not None
                and order_id in badge_unfinished_projections
            }
            if badge_unfinished_projections is not None
            else {}
        )
        missing_projection_orders = [
            order
            for order in candidate_orders
            if int(order.id) not in candidate_projection
        ]
        if missing_projection_orders:
            candidate_projection.update(
                build_order_business_statuses(
                    db,
                    missing_projection_orders,
                    include_finance=has_permission(user, "finance.view"),
                )
            )
        candidate_order_map = {int(order.id): order for order in candidate_orders}
        matched_ids = [
            order_id
            for order_id in candidate_ids
            if (
                (
                    resolved_scope != "active"
                    or candidate_projection.get(order_id, {}).get("business_status")
                    != "completed"
                )
                and (
                    resolved_scope != "completed"
                    or candidate_projection.get(order_id, {}).get("business_status")
                    == "completed"
                )
                and (
                    not derived_status_filter
                    or candidate_projection.get(order_id, {}).get("business_status")
                    in derived_status_filter
                    or (
                        order_id in candidate_order_map
                        and candidate_order_map[order_id].status in raw_status_filters
                    )
                )
            )
        ]
        total = len(matched_ids)
        page_ids = matched_ids[(page - 1) * page_size : page * page_size]
    else:
        total = (
            db.scalar(select(func.count()).select_from(ids_query.subquery())) or 0
        )
        page_ids = list(
            db.scalars(
                ids_query.offset((page - 1) * page_size).limit(page_size)
            ).all()
        )

    orders: list[Order] = []
    if page_ids:
        if (
            detail_level == "summary"
            and needs_business_projection
            and all(item_id in candidate_order_map for item_id in page_ids)
        ):
            orders = [
                candidate_order_map[item_id]
                for item_id in page_ids
                if item_id in candidate_order_map
            ]
        else:
            load_options = [selectinload(Order.items)]
            if detail_level == "full":
                load_options = [
                    selectinload(Order.items)
                    .selectinload(OrderItem.product)
                    .selectinload(Product.drawings),  # type: ignore[attr-defined]
                    selectinload(Order.items)
                    .selectinload(OrderItem.product)
                    .selectinload(Product.mold_tool),  # type: ignore[attr-defined]
                ]
            loaded = db.scalars(
                select(Order)
                .options(*load_options)
                .where(Order.id.in_(page_ids))
            ).all()
            order_map = {int(order.id): order for order in loaded}
            orders = [
                order_map[item_id] for item_id in page_ids if item_id in order_map
            ]

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
    business_projections = (
        {
            order_id: candidate_projection[order_id]
            for order_id in page_ids
            if order_id in candidate_projection
        }
        if needs_business_projection
        else build_order_business_statuses(
            db,
            orders,
            include_finance=has_permission(user, "finance.view"),
        )
    )
    full_response_context = (
        _build_full_order_response_context(
            db,
            orders,
            user,
            business_projections=business_projections,
        )
        if detail_level == "full"
        else None
    )
    unfinished_total = 0
    if include_unfinished_total:
        can_reuse_global_candidate_projection = bool(
            needs_business_projection
            and not requested_customer_ids
            and not search_keyword
            and not (order_number and order_number.strip())
            and not (customer_name and customer_name.strip())
            and order_date is None
            and date_from is None
            and date_to is None
            and order_date_from is None
            and order_date_to is None
            and delivery_date_from is None
            and delivery_date_to is None
            and not (customer_po and customer_po.strip())
            and not (product_code and product_code.strip())
            and not (product_name and product_name.strip())
            and not (specification and specification.strip())
        )
        if badge_unfinished_ids is not None and badge_unfinished_projections is not None:
            unfinished_ids = badge_unfinished_ids
            unfinished_projections = badge_unfinished_projections
        elif can_reuse_global_candidate_projection:
            unfinished_ids = [
                int(order.id) for order in candidate_orders if order.items
            ]
            unfinished_projections = candidate_projection
        else:
            unfinished_ids_query = select(Order.id).where(
                Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
                Order.items.any(),
            )
            if is_customer_scope_restricted:
                unfinished_ids_query = unfinished_ids_query.where(
                    Order.customer_id.in_(scoped_customer_ids)
                )
            unfinished_ids = list(db.scalars(unfinished_ids_query).all())
            unfinished_orders = (
                list(
                    db.scalars(
                        select(Order)
                        .options(*_business_status_projection_load_options())
                        .where(Order.id.in_(unfinished_ids))
                    ).all()
                )
                if unfinished_ids
                else []
            )
            unfinished_projections = build_order_business_statuses(
                db,
                unfinished_orders,
                include_finance=has_permission(user, "finance.view"),
            )
        unfinished_total = sum(
            1
            for order_id in unfinished_ids
            if unfinished_projections.get(order_id, {}).get("business_status")
            in _DERIVED_STATUS_FILTER_GROUPS["unfinished"]
        )
    return {
        "total": total,
        "unfinished_total": unfinished_total,
        "page": page,
        "page_size": page_size,
        "items": [
            (
                _order_list_summary_response(
                    order,
                    user,
                    customer_name=customer_names.get(order.customer_id),
                    display_registry=display_registry,
                    business_projection=business_projections.get(int(order.id)),
                )
                if detail_level == "summary"
                else _order_response(
                    order,
                    user,
                    db=db,
                    customer_name=customer_names.get(order.customer_id),
                    display_registry=display_registry,
                    **_full_order_response_kwargs(
                        full_response_context, int(order.id)
                    ),
                )
            )
            for order in orders
        ],
    }


@router.get("/cost-readiness")
def list_order_cost_readiness(
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
    _cost_user: User = Depends(can_view_cost),
) -> dict:
    """List current order items whose frozen estimated cost still has gaps.

    This endpoint is advisory and read-only. It deliberately ignores legacy
    orders without a P1-28C1 snapshot instead of backfilling current rules as
    historical facts.
    """

    latest_versions = (
        select(
            SalesOrderItemEstimatedCostSnapshot.sales_order_item_id.label("item_id"),
            func.max(
                SalesOrderItemEstimatedCostSnapshot.snapshot_version
            ).label("snapshot_version"),
        )
        .group_by(SalesOrderItemEstimatedCostSnapshot.sales_order_item_id)
        .subquery()
    )
    query = (
        select(
            SalesOrderItemEstimatedCostSnapshot,
            OrderItem,
            Order,
            Customer,
        )
        .join(
            latest_versions,
            and_(
                latest_versions.c.item_id
                == SalesOrderItemEstimatedCostSnapshot.sales_order_item_id,
                latest_versions.c.snapshot_version
                == SalesOrderItemEstimatedCostSnapshot.snapshot_version,
            ),
        )
        .join(
            OrderItem,
            OrderItem.id
            == SalesOrderItemEstimatedCostSnapshot.sales_order_item_id,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            SalesOrderItemEstimatedCostSnapshot.calculation_status
            != "calculated",
            Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
        )
        .order_by(
            Order.created_at.desc(),
            Order.id.desc(),
            OrderItem.item_sequence.asc(),
            OrderItem.id.asc(),
        )
    )
    if not has_unrestricted_customer_access(user, db):
        query = query.where(Order.customer_id.in_(customer_scope_ids(user, db)))

    candidates = list(db.execute(query).all())
    order_ids = {int(row[2].id) for row in candidates}
    orders = (
        list(
            db.scalars(
                select(Order)
                .options(selectinload(Order.items))
                .where(Order.id.in_(order_ids))
            ).all()
        )
        if order_ids
        else []
    )
    business_projections = build_order_business_statuses(
        db,
        orders,
        include_finance=False,
    )

    items: list[dict[str, object]] = []
    category_counts = {item["code"]: 0 for item in COST_GAP_CATEGORIES}
    for snapshot, item, order, customer in candidates:
        if (
            business_projections.get(int(order.id), {}).get("business_status")
            == "completed"
        ):
            continue
        expected_reference = (
            (item.item_order_number or "").strip()
            or f"order-{int(item.order_id)}-item-{int(item.id)}"
        )
        if snapshot.order_item_reference_snapshot != expected_reference:
            continue
        missing_items = load_cost_missing_items(snapshot.missing_items_json)
        categories = classify_cost_gaps(missing_items)
        for category in categories:
            category_counts[category["code"]] += 1
        items.append(
            {
                "order_id": int(order.id),
                "customer_id": int(order.customer_id),
                "customer_name": customer.name,
                "customer_po": order.customer_po,
                "order_date": order.order_date,
                "item_id": int(item.id),
                "item_sequence": item.item_sequence,
                "product_code": item.snapshot_product_code,
                "product_name": item.snapshot_product_name,
                "snapshot_version": int(snapshot.snapshot_version),
                "categories": [
                    {"code": category["code"], "label": category["label"]}
                    for category in categories
                ],
                "category_codes": [category["code"] for category in categories],
                "missing_details": missing_items,
            }
        )

    total_items = len(items)
    visible_items = items[:limit]
    return {
        "scope_label": "仅统计已有预计成本快照的当前订单；旧订单不回填",
        "total_items": total_items,
        "returned_items": len(visible_items),
        "truncated": total_items > len(visible_items),
        "categories": [
            {**category, "count": category_counts[category["code"]]}
            for category in COST_GAP_CATEGORIES
            if category_counts[category["code"]]
        ],
        "items": visible_items,
    }


@router.get("/cost-review")
def list_order_cost_review(
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
    _cost_user: User = Depends(can_view_cost),
) -> dict:
    """List current frozen estimates that need an internal margin review.

    The result reuses the P1-28C2 health classifier. It is advisory and
    read-only: healthy rows stay in the summary, while incomplete estimates
    remain exclusively in the cost-readiness list.
    """

    latest_versions = (
        select(
            SalesOrderItemEstimatedCostSnapshot.sales_order_item_id.label("item_id"),
            func.max(
                SalesOrderItemEstimatedCostSnapshot.snapshot_version
            ).label("snapshot_version"),
        )
        .group_by(SalesOrderItemEstimatedCostSnapshot.sales_order_item_id)
        .subquery()
    )
    query = (
        select(
            SalesOrderItemEstimatedCostSnapshot,
            OrderItem,
            Order,
            Customer,
        )
        .join(
            latest_versions,
            and_(
                latest_versions.c.item_id
                == SalesOrderItemEstimatedCostSnapshot.sales_order_item_id,
                latest_versions.c.snapshot_version
                == SalesOrderItemEstimatedCostSnapshot.snapshot_version,
            ),
        )
        .join(
            OrderItem,
            OrderItem.id
            == SalesOrderItemEstimatedCostSnapshot.sales_order_item_id,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            SalesOrderItemEstimatedCostSnapshot.calculation_status
            == "calculated",
            Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
        )
        .order_by(
            Order.created_at.desc(),
            Order.id.desc(),
            OrderItem.item_sequence.asc(),
            OrderItem.id.asc(),
        )
    )
    if not has_unrestricted_customer_access(user, db):
        query = query.where(Order.customer_id.in_(customer_scope_ids(user, db)))

    candidates = list(db.execute(query).all())
    order_ids = {int(row[2].id) for row in candidates}
    orders = (
        list(
            db.scalars(
                select(Order)
                .options(selectinload(Order.items))
                .where(Order.id.in_(order_ids))
            ).all()
        )
        if order_ids
        else []
    )
    business_projections = build_order_business_statuses(
        db,
        orders,
        include_finance=False,
    )

    summary_definitions = (
        ("estimated_loss", "预计亏损", "red"),
        ("very_low", "利润空间很低", "red"),
        ("review", "建议复核", "orange"),
        ("sale_missing", "售价待完善", "orange"),
        ("healthy", "预计正常", "green"),
    )
    summary_counts = {code: 0 for code, _label, _tone in summary_definitions}
    severity = {
        "estimated_loss": 0,
        "very_low": 1,
        "review": 2,
        "sale_missing": 3,
    }
    items: list[dict[str, object]] = []
    for snapshot, item, order, customer in candidates:
        if (
            business_projections.get(int(order.id), {}).get("business_status")
            == "completed"
        ):
            continue
        expected_reference = (
            (item.item_order_number or "").strip()
            or f"order-{int(item.order_id)}-item-{int(item.id)}"
        )
        if snapshot.order_item_reference_snapshot != expected_reference:
            continue
        health = classify_estimated_cost_health(snapshot, item.subtotal)
        health_code = str(health["estimated_cost_health_code"])
        if health_code not in summary_counts:
            continue
        summary_counts[health_code] += 1
        if health_code == "healthy":
            continue
        items.append(
            {
                "order_id": int(order.id),
                "customer_id": int(order.customer_id),
                "customer_name": customer.name,
                "customer_po": order.customer_po,
                "order_date": order.order_date,
                "item_id": int(item.id),
                "item_sequence": item.item_sequence,
                "product_code": item.snapshot_product_code,
                "product_name": item.snapshot_product_name,
                "sale_amount": str(item.subtotal),
                "estimated_order_total_cost": str(
                    snapshot.estimated_order_total_cost
                ),
                "estimated_gross_profit": health["estimated_gross_profit"],
                "estimated_margin_rate": health["estimated_margin_rate"],
                "health_code": health_code,
                "health_label": health["estimated_cost_health_label"],
                "health_tone": health["estimated_cost_health_tone"],
                "health_version": health["estimated_cost_health_version"],
            }
        )

    # Python sorting is stable, preserving newest-order-first within one level.
    items.sort(key=lambda row: severity[str(row["health_code"])])
    total_items = len(items)
    visible_items = items[:limit]
    return {
        "scope_label": "当前订单冻结预计成本，仅供内部复核，不是实际利润",
        "evaluated_items": sum(summary_counts.values()),
        "total_items": total_items,
        "returned_items": len(visible_items),
        "truncated": total_items > len(visible_items),
        "summary": [
            {
                "code": code,
                "label": label,
                "tone": tone,
                "count": summary_counts[code],
            }
            for code, label, tone in summary_definitions
        ],
        "items": visible_items,
    }


@router.get("/customer-options")
def list_order_customer_options(
    keyword: str | None = None,
    scope: Literal["active", "completed", "cancelled", "all"] = "active",
    stage: list[str] | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """List only customers that have orders in the selected business scope.

    This is intentionally separate from ``/api/customers``: its keyword only
    narrows customer identity fields and can never broaden the order scope.
    """

    order_query = select(Order).options(selectinload(Order.items))
    scoped_customer_ids = customer_scope_ids(user, db)
    if not has_unrestricted_customer_access(user, db):
        order_query = order_query.where(Order.customer_id.in_(scoped_customer_ids))
    if scope in {"active", "completed"}:
        order_query = order_query.where(
            Order.status.notin_(_BUSINESS_EXCLUDED_STATUSES),
        )
    elif scope == "cancelled":
        order_query = order_query.where(Order.status.in_(_BUSINESS_EXCLUDED_STATUSES))

    candidates = list(db.scalars(order_query).all())
    stage_values = tuple(
        dict.fromkeys(value.strip() for value in (stage or []) if value.strip())
    )
    selected_stages: set[str] = set()
    for value in stage_values:
        if value in DERIVED_BUSINESS_STATUSES or value == "pending_confirmation":
            selected_stages.add(value)
        elif value in _DERIVED_STATUS_FILTER_GROUPS:
            selected_stages.update(_DERIVED_STATUS_FILTER_GROUPS[value])
    needs_business_projection = bool(selected_stages) or scope in {"active", "completed"}
    if needs_business_projection:
        projections = build_order_business_statuses(
            db, candidates, include_finance=has_permission(user, "finance.view")
        )
        candidates = [
            order
            for order in candidates
            if (
                (scope != "active" or projections.get(int(order.id), {}).get("business_status") != "completed")
                and (scope != "completed" or projections.get(int(order.id), {}).get("business_status") == "completed")
                and (
                    not selected_stages
                    or projections.get(int(order.id), {}).get("business_status") in selected_stages
                )
            )
        ]

    order_counts: dict[int, int] = {}
    for order in candidates:
        order_counts[order.customer_id] = order_counts.get(order.customer_id, 0) + 1
    customer_ids = set(order_counts)
    customer_query = select(Customer).where(Customer.id.in_(customer_ids))
    customer_identity_filter = customer_identity_search_clause(keyword)
    if customer_identity_filter is not None:
        customer_query = customer_query.where(customer_identity_filter)
    customer_query = customer_query.order_by(Customer.customer_number, Customer.id)
    total = db.scalar(select(func.count()).select_from(customer_query.subquery())) or 0
    customers = db.scalars(
        customer_query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": customer.id,
                "name": customer.name,
                "customer_code": customer.customer_code,
                "is_active": customer.is_active,
                "order_count": order_counts.get(customer.id, 0),
            }
            for customer in customers
        ],
    }


@router.get("/customer-heat")
def list_order_customer_heat(
    keyword: str | None = None,
    customer_id: int | None = None,
    stage: list[str] | None = Query(default=None),
    order_date_from: date | None = None,
    order_date_to: date | None = None,
    delivery_date_from: date | None = None,
    delivery_date_to: date | None = None,
    sort_by: Literal["heat", "recency", "frequency", "annual_amount"] = "heat",
    sort_direction: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return a permission-scoped, read-only customer activity projection."""

    resolved_as_of = beijing_today()
    include_amounts = _can_view_order_sales_amount(user)
    if sort_by == "annual_amount" and not include_amounts:
        raise HTTPException(status_code=403, detail="无订单金额查看权限")
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    if customer_id is not None:
        require_customer_access(customer_id, current_user=user, db=db)
    selected_stages: set[str] = set()
    for value in (stage or []):
        normalized = value.strip()
        if normalized in DERIVED_BUSINESS_STATUSES or normalized == "pending_confirmation":
            selected_stages.add(normalized)
        elif normalized in _DERIVED_STATUS_FILTER_GROUPS:
            selected_stages.update(_DERIVED_STATUS_FILTER_GROUPS[normalized])
    filters_requested = bool(
        customer_id is not None
        or selected_stages
        or order_date_from is not None
        or order_date_to is not None
        or delivery_date_from is not None
        or delivery_date_to is not None
    )
    eligible_customer_ids = (
        customer_ids_matching_heat_filters(
            db,
            as_of=resolved_as_of,
            visible_customer_ids=visible_customer_ids,
            customer_id=customer_id,
            selected_stages=selected_stages,
            order_date_from=order_date_from,
            order_date_to=order_date_to,
            delivery_date_from=delivery_date_from,
            delivery_date_to=delivery_date_to,
            include_finance_status=has_permission(user, "finance.view"),
        )
        if filters_requested
        else None
    )
    result = list_customer_heat(
        db,
        as_of=resolved_as_of,
        visible_customer_ids=visible_customer_ids,
        include_amounts=include_amounts,
        include_finance_status=has_permission(user, "finance.view"),
        keyword=keyword,
        sort_by=sort_by,
        sort_direction=sort_direction,
        page=page,
        page_size=page_size,
        eligible_customer_ids=eligible_customer_ids,
    )
    return {
        **result,
        "as_of": resolved_as_of,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "amount_visible": include_amounts,
        "threshold_version": CUSTOMER_HEAT_THRESHOLD_VERSION,
        "threshold_status": CUSTOMER_HEAT_THRESHOLD_STATUS,
        "threshold_contract": customer_heat_threshold_contract(),
    }


@router.get("/customer-heat/{customer_id}/orders")
def list_customer_heat_orders(
    customer_id: int,
    keyword: str | None = None,
    scope: Literal["active", "completed", "cancelled", "all"] = "active",
    stage: list[str] | None = Query(default=None),
    order_date_from: date | None = None,
    order_date_to: date | None = None,
    delivery_date_from: date | None = None,
    delivery_date_to: date | None = None,
    sort_by: Literal["customer_name", "order_date", "delivery_date"] | None = None,
    sort_direction: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Lazily reuse the existing order-summary contract for one heat group."""

    require_customer_access(customer_id, current_user=user, db=db)
    customer = db.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.status == "active",
            Customer.is_active.is_(True),
        )
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在或已停用")
    result = list_orders(
        customer_id=customer_id,
        customer_ids=None,
        keyword=keyword,
        order_number=None,
        customer_po=None,
        product_code=None,
        product_name=None,
        specification=None,
        customer_name=None,
        order_date=None,
        date_from=None,
        date_to=None,
        order_date_from=order_date_from,
        order_date_to=order_date_to,
        delivery_date_from=delivery_date_from,
        delivery_date_to=delivery_date_to,
        status_filter=None,
        scope=scope,
        stage=stage,
        sort_by=sort_by,
        sort_direction=sort_direction,
        detail_level="summary",
        include_unfinished_total=False,
        page=page,
        page_size=page_size,
        db=db,
        user=user,
    )
    return {
        "customer_id": customer_id,
        "customer_name": customer.name,
        "scope": scope,
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
        "items": result["items"],
    }


@router.get("/fulfillment-reminders")
def get_order_fulfillment_reminders(
    customer_id: int = Query(gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return active internal production reminders for order entry."""

    require_customer_access(customer_id, user, db)
    return list_order_production_reminders(
        db,
        customer_id=customer_id,
        page=page,
        page_size=page_size,
    )


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


def _draft_preview_cutting_mode(product: Product) -> str:
    box_style = (product.box_style or "").strip()
    if box_style not in CUTTING_MODE_BOX_STYLES:
        return DEFAULT_CUTTING_MODE
    mode = (product.default_cutting_mode or "").strip()
    return mode if cutting_factor(mode) > 1 or mode == DEFAULT_CUTTING_MODE else DEFAULT_CUTTING_MODE


def _draft_preview_component_specs(product: Product) -> list[dict[str, int | str]]:
    if _is_telescoping_product(product):
        return [
            {"component_type": "cover", "pieces_per_box": 1},
            {"component_type": "base", "pieces_per_box": 1},
        ]
    pieces_per_box = max(
        int(
            product.pieces_per_box
            or (2 if (product.splice_mode or "").strip().lower() == "double" else 1)
        ),
        1,
    )
    return [{"component_type": "whole", "pieces_per_box": pieces_per_box}]


@router.post("/inventory-draft-preview")
def preview_order_inventory_draft(
    payload: InventoryDraftPreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    """Return the authoritative read-only quantity contract for a new-order draft.

    This endpoint deliberately accepts the complete draft.  Shared lots are
    simulated once, in line order, so a physical balance cannot be counted in
    two PDF rows.  It creates no order, reservation, movement, or inventory
    mutation; formal save still performs the existing transactional preflight.
    """

    if not has_permission(user, "warehouse.view"):
        raise HTTPException(status_code=403, detail="当前账号没有仓库库存查看权限")
    require_customer_access(payload.customer_id, user, db)

    client_line_ids: set[str] = set()
    for index, draft_item in enumerate(payload.items, start=1):
        client_line_id = draft_item.client_line_id.strip()
        if client_line_id in client_line_ids:
            raise HTTPException(
                status_code=409,
                detail=f"第{index}条明细 client_line_id 重复",
            )
        client_line_ids.add(client_line_id)

    products: list[Product] = []
    preflight_items: list[OrderItemCreate] = []
    resolved_products: dict[int, Product] = {}
    for index, draft_item in enumerate(payload.items, start=1):
        product = db.get(Product, draft_item.product_id)
        if product is None or product.deleted_at is not None or not product.is_active:
            raise HTTPException(
                status_code=404,
                detail=f"第{index}条明细产品不存在或已停用",
            )
        if product.customer_id != payload.customer_id:
            raise HTTPException(
                status_code=409,
                detail=f"第{index}条明细产品不属于所选客户",
            )
        products.append(product)
        if is_composite_product(product):
            if draft_item.reservation_plan.finished or draft_item.reservation_plan.semi:
                raise HTTPException(
                    status_code=409,
                    detail=f"第{index}条组合产品不能建立父项库存抵扣计划",
                )
            continue
        preflight_index = len(preflight_items) + 1
        preflight_items.append(
            OrderItemCreate(
                client_line_id=draft_item.client_line_id,
                product_id=draft_item.product_id,
                quantity=draft_item.quantity,
                unit_price=Decimal("0"),
                material=draft_item.material,
                flute_type=draft_item.flute_type,
                reservation_plan=draft_item.reservation_plan,
            )
        )
        resolved_products[preflight_index] = product

    if preflight_items:
        try:
            _preflight_reservation_plans(
                db,
                customer_id=payload.customer_id,
                payload_items=preflight_items,
                resolved_products=resolved_products,
            )
        except WarehouseInventoryError as error:
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
            ) from error

    used_stock_by_lot: dict[int, int] = {}
    response_items: list[dict] = []
    preflight_by_line = {row.client_line_id: row for row in preflight_items}
    for draft_item, product in zip(payload.items, products, strict=True):
        order_quantity = int(draft_item.quantity)
        if is_composite_product(product):
            response_items.append(
                {
                    "client_line_id": draft_item.client_line_id,
                    "product_id": product.id,
                    "order_quantity": order_quantity,
                    "interaction_state": "decision_required",
                    "coverage_state": "unsupported",
                    "message": "组合产品需进入订单明细后按父件和组件分别计算库存与报料",
                    "requisition_unit": "张",
                    "requisition_components": [],
                }
            )
            continue

        candidates = finished_inventory_candidates_for_product(
            db,
            customer_id=payload.customer_id,
            product_id=product.id,
        )
        candidate_available_quantity = sum(
            max(
                int(lot.quantity_available or 0)
                - used_stock_by_lot.get(lot.id, 0),
                0,
            )
            for lot in candidates
        )
        remaining_boxes = order_quantity
        planned_finished_quantity = 0
        for entry in draft_item.reservation_plan.finished:
            if remaining_boxes <= 0:
                break
            lot = db.get(InventoryLot, entry.lot_id)
            if lot is None:
                continue
            used_quantity = used_stock_by_lot.get(lot.id, 0)
            available_quantity = max(
                int(lot.quantity_available or 0) - used_quantity,
                0,
            )
            allocated_quantity = min(
                int(entry.requested_qty),
                remaining_boxes,
                available_quantity,
            )
            if allocated_quantity <= 0:
                continue
            used_stock_by_lot[lot.id] = used_quantity + allocated_quantity
            planned_finished_quantity += allocated_quantity
            remaining_boxes -= allocated_quantity

        production_required_quantity = max(
            order_quantity - planned_finished_quantity,
            0,
        )
        cutting_mode = _draft_preview_cutting_mode(product)
        component_rows: list[dict] = []
        for component in _draft_preview_component_specs(product):
            component_type = str(component["component_type"])
            pieces_per_box = int(component["pieces_per_box"])
            required_pieces = required_piece_quantity(
                production_required_quantity,
                pieces_per_box,
            )
            remaining_pieces = required_pieces
            semi_planned_pieces = 0
            for entry in draft_item.reservation_plan.semi:
                if entry.component_type != component_type or remaining_pieces <= 0:
                    continue
                lot = db.get(InventoryLot, entry.lot_id)
                detail = lot.semi_finished_detail if lot is not None else None
                if lot is None or detail is None:
                    continue
                output_per_stock_sheet = max(
                    int(detail.stock_yield_per_sheet or 1),
                    1,
                )
                from app.services.sheet_cut_plan import rectangular_cut_plan
                cutting_plan = rectangular_cut_plan(db, lot, product, _preflight_semi_signature(
                    customer_id=payload.customer_id, product=product,
                    item_payload=preflight_by_line[draft_item.client_line_id], component_type=component_type,
                    stock_yield_per_sheet=cutting_factor(product.default_cutting_mode),
                ))
                if cutting_plan:
                    output_per_stock_sheet = cutting_plan["yield_factor"]
                used_sheets = used_stock_by_lot.get(lot.id, 0)
                available_sheets = max(
                    int(lot.quantity_available or 0) - used_sheets,
                    0,
                )
                allocated_pieces = min(
                    int(entry.requested_qty),
                    remaining_pieces,
                    available_sheets * output_per_stock_sheet,
                )
                if allocated_pieces <= 0:
                    continue
                consumed_sheets = (
                    allocated_pieces + output_per_stock_sheet - 1
                ) // output_per_stock_sheet
                used_stock_by_lot[lot.id] = used_sheets + consumed_sheets
                semi_planned_pieces += allocated_pieces
                remaining_pieces -= allocated_pieces

            component_rows.append(
                {
                    "component_type": component_type,
                    "pieces_per_box": pieces_per_box,
                    "required_piece_quantity": required_pieces,
                    "semi_planned_requirement_quantity": semi_planned_pieces,
                    "remaining_required_piece_quantity": remaining_pieces,
                    "cutting_mode": cutting_mode,
                    "cutting_factor": cutting_factor(cutting_mode),
                    "requisition_sheet_quantity": purchase_sheet_quantity(
                        remaining_pieces,
                        0,
                        cutting_mode,
                    ),
                    "requisition_unit": "张",
                }
            )

        if planned_finished_quantity == order_quantity and planned_finished_quantity > 0:
            coverage_state = "full"
        elif planned_finished_quantity > 0:
            coverage_state = "partial"
        elif candidate_available_quantity <= 0:
            coverage_state = "none"
        else:
            coverage_state = "candidates_unplanned"
        interaction_state = (
            "skipped"
            if coverage_state == "candidates_unplanned" and draft_item.finished_skipped
            else (
                "decision_required"
                if coverage_state == "candidates_unplanned"
                else "ready"
            )
        )
        response_items.append(
            {
                "client_line_id": draft_item.client_line_id,
                "product_id": product.id,
                "order_quantity": order_quantity,
                "interaction_state": interaction_state,
                "coverage_state": coverage_state,
                "finished_candidate_available_quantity": candidate_available_quantity,
                "finished_planned_quantity": planned_finished_quantity,
                "production_required_quantity": production_required_quantity,
                "shortage_quantity": production_required_quantity,
                "required_piece_quantity": sum(
                    int(row["required_piece_quantity"]) for row in component_rows
                ),
                "semi_planned_requirement_quantity": sum(
                    int(row["semi_planned_requirement_quantity"])
                    for row in component_rows
                ),
                "remaining_required_piece_quantity": sum(
                    int(row["remaining_required_piece_quantity"])
                    for row in component_rows
                ),
                "requisition_sheet_quantity": sum(
                    int(row["requisition_sheet_quantity"])
                    for row in component_rows
                ),
                "requisition_unit": "张",
                "cutting_mode": cutting_mode,
                "cutting_factor": cutting_factor(cutting_mode),
                "requisition_components": component_rows,
            }
        )

    return {
        "customer_id": payload.customer_id,
        "calculation_scope": "read_only_new_order_draft",
        "items": response_items,
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
            "source_lines": trusted_claims["source_lines"],
        },
    )


@router.post("/draft-manual-rematch")
def rematch_manual_order_draft(
    payload: ManualDraftRematchRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(can_create),
) -> dict:
    """Turn a failed PDF preview into an explicitly checked manual draft.

    The original signed file identity remains authoritative.  Only the order
    facts typed by the operator are copied into the rematch request; product
    identity is resolved again against the selected customer's active common
    boxes before a new save token is issued.
    """

    trusted_claims = _decode_pdf_preview_safety_token(
        payload.preview_safety_token,
        _user,
    )
    draft_source_name = str(payload.draft.get("source_name") or "").strip()
    trusted_source_name = str(trusted_claims["source_name"] or "").strip()
    draft_source_hash = str(payload.draft.get("file_hash") or "").strip().casefold()
    trusted_source_hash = str(trusted_claims["source_hash"] or "").strip().casefold()
    if (draft_source_name or trusted_source_name) and draft_source_name != trusted_source_name:
        raise _pdf_preview_token_error("PDF 预览 token 与草稿文件名不一致，请重新预览")
    if (draft_source_hash or trusted_source_hash) and draft_source_hash != trusted_source_hash:
        raise _pdf_preview_token_error("PDF 预览 token 与草稿文件哈希不一致，请重新预览")
    if trusted_claims["recognition_status"] not in {"failed", "needs_confirmation"}:
        raise HTTPException(status_code=409, detail="当前 PDF 已正常识别，无需改用人工录入")

    customer_po = str(payload.draft.get("customer_po") or "").strip()
    if not customer_po:
        raise HTTPException(status_code=400, detail="按原 PDF 人工录入时必须填写客户单号")
    raw_items = payload.draft.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 500:
        raise HTTPException(status_code=400, detail="按原 PDF 人工录入必须包含 1 至 500 条明细")

    manual_items: list[dict] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"第{index}条人工明细格式无效")
        raw_code = str(raw.get("raw_product_code") or raw.get("product_code") or "").strip()
        raw_name = str(raw.get("raw_product_name") or raw.get("product_name") or "").strip()
        if not raw_code and not raw_name:
            raise HTTPException(status_code=400, detail=f"第{index}条人工明细必须填写存货编码或产品名称")
        try:
            quantity = Decimal(str(raw.get("quantity")))
        except (ArithmeticError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"第{index}条人工明细数量无效") from None
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise HTTPException(status_code=400, detail=f"第{index}条人工明细数量必须为正整数")
        unit_price = raw.get("unit_price")
        if unit_price not in (None, ""):
            try:
                price = Decimal(str(unit_price))
            except (ArithmeticError, TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"第{index}条人工明细单价无效") from None
            if price < 0:
                raise HTTPException(status_code=400, detail=f"第{index}条人工明细单价不能小于 0")
        manual_items.append(
            {
                "line_no": raw.get("line_no") or index,
                "raw_product_code": raw_code,
                "product_code": raw_code,
                "raw_product_name": raw_name,
                "raw_spec_model": str(raw.get("raw_spec_model") or raw.get("specification") or "").strip(),
                "quantity": int(quantity),
                "unit": str(raw.get("unit") or "").strip() or None,
                "unit_price": unit_price,
                "delivery_date": raw.get("delivery_date") or payload.draft.get("delivery_date"),
                "is_new_product": False,
            }
        )

    trusted_draft = {
        "source_name": trusted_claims["source_name"],
        "file_hash": trusted_claims["source_hash"],
        "customer_po": customer_po,
        "order_date": payload.draft.get("order_date"),
        "delivery_date": payload.draft.get("delivery_date"),
        "recognition_status": "needs_confirmation",
        "parse_status": "needs_confirmation",
        "manual_entry": True,
        "customer_route": {"status": "manual"},
        "integrity_check": {
            "integrity_status": "manual_confirmed",
            "integrity_errors": [],
            "manual_notice": "操作员已逐项对照原 PDF 人工录入，系统未自动补写订单事实。",
        },
        "items": manual_items,
        "warnings": ["本草稿由操作员逐项对照原 PDF 人工录入，请在保存前再次核对。"],
    }
    result = _match_pdf_preview_for_user(
        db,
        trusted_draft,
        _user,
        customer_id=payload.customer_id,
    )
    result["manual_entry"] = True
    return _finalize_pdf_preview_for_user(
        result,
        _user,
        state_overrides={
            "recognition_status": "needs_confirmation",
            "customer_route_status": "manual",
            "customer_match_status": "matched",
            "integrity_status": "manual_confirmed",
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
    from app.models.delivery_backlog import DeliveryBacklog
    if db.scalar(select(DeliveryBacklog.id).join(OrderItem,OrderItem.id==DeliveryBacklog.order_item_id)
            .where(OrderItem.order_id.in_(order_ids)).limit(1)):
        labels.append('预送货待补送及实际发货追溯')
    from app.models.raw_purchase_plan import RawPurchaseDemand
    if db.scalar(select(RawPurchaseDemand.id).join(OrderItem,OrderItem.id==RawPurchaseDemand.order_item_id)
            .where(OrderItem.order_id.in_(order_ids)).limit(1)):
        labels.append('统一原片采购及分配历史')
    if db.scalar(
        select(func.count())
        .select_from(ExternalPackagingPurchaseItem)
        .where(ExternalPackagingPurchaseItem.sales_order_id.in_(order_ids))
    ):
        labels.append("外购包材采购历史")
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


def _purge_fully_cancelled_external_purchase_history(
    db: Session,
    *,
    orders: list[Order],
    user: User,
    request: Request | None,
    batch_id: str | None,
) -> list[dict]:
    """Remove only cancelled, never-received purchase facts before order deletion.

    The database migration adds a short-lived authorization gate.  It rejects
    authorization unless every supplier purchase in the batch has a
    cancellation fact and no receipt fact exists.  A durable operation audit is
    written before the immutable purchase rows are removed.
    """

    order_ids = [int(order.id) for order in orders]
    batches = list(
        db.scalars(
            select(ExternalPackagingPurchaseBatch)
            .options(
                selectinload(
                    ExternalPackagingPurchaseBatch.purchase_orders
                ).selectinload(ExternalPackagingPurchaseOrder.items)
            )
            .where(ExternalPackagingPurchaseBatch.sales_order_id.in_(order_ids))
            .order_by(ExternalPackagingPurchaseBatch.id)
            .with_for_update(of=ExternalPackagingPurchaseBatch)
        ).unique().all()
    )
    if not batches:
        return []
    purchase_orders = [
        purchase for batch in batches for purchase in batch.purchase_orders
    ]
    purchase_ids = {int(purchase.id) for purchase in purchase_orders}
    cancellation_rows = list(
        db.scalars(
            select(ExternalPackagingPurchaseCancellation).where(
                ExternalPackagingPurchaseCancellation.purchase_order_id.in_(
                    purchase_ids
                )
            )
        ).all()
    )
    cancelled_ids = {int(row.purchase_order_id) for row in cancellation_rows}
    active = [
        purchase.purchase_number
        for purchase in purchase_orders
        if int(purchase.id) not in cancelled_ids
    ]
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                "订单组仍有未撤回的外购包材采购单："
                f"{'、'.join(active)}。请先撤回到未报料后再删除订单组。"
            ),
        )
    received_purchase_ids = {
        int(value)
        for value in db.scalars(
            select(ExternalPackagingReceipt.purchase_order_id).where(
                ExternalPackagingReceipt.purchase_order_id.in_(purchase_ids)
            )
        ).all()
    }
    if received_purchase_ids:
        numbers = [
            purchase.purchase_number
            for purchase in purchase_orders
            if int(purchase.id) in received_purchase_ids
        ]
        raise HTTPException(
            status_code=409,
            detail=(
                "外购包材采购单已有实收历史，不能删除订单组："
                f"{'、'.join(numbers)}。请保留订单并改为作废或归档。"
            ),
        )
    cancellations_by_purchase_id = {
        int(row.purchase_order_id): row for row in cancellation_rows
    }
    snapshots: list[dict] = []
    orders_by_id = {int(order.id): order for order in orders}
    for purchase_batch in batches:
        order = orders_by_id[int(purchase_batch.sales_order_id)]
        purchase_snapshots = []
        for purchase in purchase_batch.purchase_orders:
            cancellation = cancellations_by_purchase_id[int(purchase.id)]
            purchase_snapshots.append(
                {
                    "purchase_order_id": int(purchase.id),
                    "purchase_number": purchase.purchase_number,
                    "supplier_id": int(purchase.supplier_id),
                    "supplier_name": purchase.supplier_name_snapshot,
                    "total_amount": str(purchase.total_amount),
                    "cancellation_source": cancellation.source,
                    "cancellation_reason": cancellation.reason,
                    "items": [
                        {
                            "purchase_item_id": int(item.id),
                            "product_code": item.supplier_product_code_snapshot,
                            "product_name": item.product_name_snapshot,
                            "purchase_quantity": str(item.purchase_quantity),
                            "purchase_unit": item.purchase_unit,
                        }
                        for item in purchase.items
                    ],
                }
            )
        snapshot = {
            "purchase_batch_id": int(purchase_batch.id),
            "sales_order_id": int(order.id),
            "sales_order_number": order.order_number,
            "purchase_orders": purchase_snapshots,
        }
        snapshots.append(snapshot)
        db.add(
            ExternalPackagingPurchasePurgeAuthorization(
                batch_id=purchase_batch.id,
                authorized_by=user.id,
                reason="订单已完整撤回到未报料，用户二次确认删除订单组",
            )
        )
        _append_order_audit(
            db,
            request=request,
            user=user,
            order=order,
            action_code="order.external_purchase.cancelled_history_purge",
            legacy_action="PURGE_CANCELLED_EXT_PURCHASE",
            description="删除订单组前清理已取消且从未实收的外购采购事实",
            details=snapshot,
            batch_id=batch_id,
        )
    db.flush()
    db.execute(
        delete(ExternalPackagingPurchaseCancellation).where(
            ExternalPackagingPurchaseCancellation.purchase_order_id.in_(purchase_ids)
        ).execution_options(synchronize_session=False)
    )
    batch_ids = {int(row.id) for row in batches}
    db.execute(
        delete(ExternalPackagingPurchaseItem).where(
            ExternalPackagingPurchaseItem.purchase_order_id.in_(purchase_ids)
        ).execution_options(synchronize_session=False)
    )
    db.execute(
        delete(ExternalPackagingPurchaseOrder).where(
            ExternalPackagingPurchaseOrder.batch_id.in_(batch_ids)
        ).execution_options(synchronize_session=False)
    )
    db.execute(
        delete(ExternalPackagingPurchaseBatch).where(
            ExternalPackagingPurchaseBatch.id.in_(batch_ids)
        ).execution_options(synchronize_session=False)
    )
    db.flush()
    return snapshots


def _flow_delete_message(labels: list[str]) -> str:
    if "外购包材采购历史" in labels:
        return "该订单已有外购包材采购历史，不能物理删除；请保留原订单用于追溯，并将订单标记为作废或归档。"
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


def _already_at_workflow_rollback_baseline(
    db: Session,
    *,
    order: Order,
    item_ids: list[int],
) -> bool:
    """Return true only when a prior rollback left no new downstream work."""

    prior_rollback = db.scalar(
        select(OperationLog.id)
        .where(
            OperationLog.entity_type == "order",
            OperationLog.entity_id == order.id,
            OperationLog.action_code == "order.workflow_rollback",
        )
        .limit(1)
    )
    if prior_rollback is None or order.status != "pending_production":
        return False
    if order.payment_status != "unpaid":
        return False
    if any(
        (
            int(item.delivered_quantity or 0) != 0
            or bool(item.is_force_closed)
            or item.material_status != "pending"
            or item.material_received_at is not None
            or item.material_received_by is not None
            or int(item.inventory_deducted_qty or 0) != 0
            or item.requisition_qty is not None
            or item.requisition_status != "未报料"
            or item.special_process != "无"
            or item.requisition_spec is not None
            or item.cardboard_len is not None
            or item.cardboard_width is not None
            or item.requisition_date is not None
            or item.supplier_delivery_time is not None
            or item.supplier_order_number is not None
            or item.requisition_remark is not None
        )
        for item in order.items
    ):
        return False
    if active_external_purchase_orders_for_order_ids(db, {order.id}):
        return False
    supplier_statuses = db.scalars(
        select(SupplierRequisitionOrder.status)
        .join(
            SupplierRequisitionOrderItem,
            SupplierRequisitionOrderItem.supplier_order_id
            == SupplierRequisitionOrder.id,
        )
        .where(SupplierRequisitionOrderItem.order_item_id.in_(item_ids))
    ).all()
    inactive_supplier_statuses = {
        _normalized_supplier_requisition_status(value)
        for value in _INACTIVE_SUPPLIER_REQUISITION_ORDER_STATUSES
    }
    if any(
        _normalized_supplier_requisition_status(value)
        not in inactive_supplier_statuses
        for value in supplier_statuses
    ):
        return False
    if db.scalar(
        select(RequisitionItem.id)
        .where(RequisitionItem.order_item_id.in_(item_ids))
        .limit(1)
    ) is not None:
        return False
    if db.scalar(
        select(DeliveryItem.id)
        .where(DeliveryItem.order_item_id.in_(item_ids))
        .limit(1)
    ) is not None:
        return False
    if db.scalar(
        select(IncomingReceiptItem.id)
        .where(
            or_(
                IncomingReceiptItem.order_id == order.id,
                IncomingReceiptItem.order_item_id.in_(item_ids),
            ),
            IncomingReceiptItem.status == "posted",
        )
        .limit(1)
    ) is not None:
        return False
    if db.scalar(
        select(InventoryReservation.id)
        .where(
            InventoryReservation.order_item_id.in_(item_ids),
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .limit(1)
    ) is not None:
        return False
    if _active_predelivery_order_ids(db, [order.id]):
        return False
    if db.scalar(
        select(TianhuaPreDeliveryImportItem.id)
        .where(TianhuaPreDeliveryImportItem.order_id == order.id)
        .limit(1)
    ) is not None:
        return False
    return True


def _ensure_statement_chain_can_be_removed_for_workflow_rollback(
    db: Session,
    statement_ids: set[int],
) -> None:
    """Allow cleanup only for unconfirmed drafts without immutable finance facts."""

    if not statement_ids:
        return
    blocked_reasons: list[str] = []
    statement_states = db.execute(
        select(
            Statement.confirmation_status,
            Statement.status,
            Statement.invoiced_amount,
            Statement.settled_amount,
        ).where(Statement.id.in_(statement_ids))
    ).all()
    if any(row.confirmation_status != "draft" for row in statement_states):
        blocked_reasons.append("对账单已确认或取消")
    if any(row.status != "unsettled" for row in statement_states):
        blocked_reasons.append("对账单已结清")
    if any(Decimal(row.invoiced_amount or 0) > 0 for row in statement_states):
        blocked_reasons.append("对账单已有开票累计")
    if any(Decimal(row.settled_amount or 0) > 0 for row in statement_states):
        blocked_reasons.append("对账单已有收款累计")
    finance_fact_models = (
        (FinanceInvoiceTask, "已生成开票任务"),
        (FinanceManualMutation, "已形成财务幂等事实"),
        (Invoice, "已登记开票"),
        (SettlementRecord, "已登记收款"),
    )
    for model, label in finance_fact_models:
        if db.scalar(
            select(model.id)
            .where(model.statement_id.in_(statement_ids))
            .limit(1)
        ) is not None:
            blocked_reasons.append(label)
    if blocked_reasons:
        raise HTTPException(
            status_code=409,
            detail=(
                "关联对账链已进入财务受控阶段（"
                + "、".join(blocked_reasons)
                + "），不能自动撤回或删除。请先按财务作废/冲销流程处理。"
            ),
        )


def _lock_orders_for_production_transition(
    db: Session,
    order_ids: list[int],
) -> dict[int, Order]:
    try:
        locked=lock_order_rows_for_production_transition(db, order_ids)
        from app.services.raw_purchase_plans import assert_no_plan
        assert_no_plan(db,list(db.scalars(select(OrderItem.id).where(OrderItem.order_id.in_(order_ids)))))
        return locked
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
        "special_process",
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
            "production_notes",
            "print_content",
        ):
            value = getattr(payload, field_name)
            if value is not None:
                changed("产品生产参数", getattr(product, field_name), value)
    return list(dict.fromkeys(changes))


def _invalidate_requisition_holds_before_item_delete(
    db: Session,
    *,
    item_ids: list[int],
    user: User,
    request: Request | None = None,
    batch_id: str | None = None,
) -> None:
    if not item_ids:
        return
    holds = db.scalars(
        select(RequisitionHold).where(
            RequisitionHold.order_item_id.in_(item_ids),
            RequisitionHold.status == "active",
        )
    ).all()
    now = utc_now_naive()
    for hold in holds:
        hold.status = "invalidated"
        hold.released_by = user.id
        hold.released_at = now
        hold.release_source = "order_item_deleted"
        hold.release_note = "订单明细删除，等候报料记录自动失效"
        hold.updated_by = user.id
        hold.version = int(hold.version or 0) + 1
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="web",
            module_code="requisition",
            action_code="requisition.hold.invalidate",
            legacy_action="INVALIDATE_REQUISITION_HOLD",
            resource="Requisition",
            entity_type="order_item",
            entity_id=hold.order_item_id_snapshot,
            object_ref=(
                f"{hold.order_number_snapshot}:"
                f"{hold.order_item_sequence_snapshot or hold.order_item_id_snapshot}"
            ),
            customer_id=hold.customer_id_snapshot,
            customer_name=hold.customer_name_snapshot,
            batch_id=batch_id,
            description="订单明细删除，等候报料记录自动失效",
            details={
                "hold_id": hold.id,
                "order_item_id": hold.order_item_id_snapshot,
                "source": "order_item_deleted",
            },
        )


def _delete_orders_in_transaction(
    db: Session,
    *,
    orders: list[Order],
    user: User,
    request: Request | None = None,
    batch_id: str | None = None,
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
    _purge_fully_cancelled_external_purchase_history(
        db,
        orders=orders,
        user=user,
        request=request,
        batch_id=batch_id,
    )
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
    _invalidate_requisition_holds_before_item_delete(
        db,
        item_ids=item_ids,
        user=user,
        request=request,
        batch_id=batch_id,
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
        for item in order.items:
            _append_order_audit(
                db,
                request=request,
                user=user,
                order=order,
                action_code="order.item.delete",
                legacy_action="DELETE",
                description="随整单删除订单明细",
                details={
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "item_id": item.id,
                    "item_order_number": item.item_order_number,
                    "product_code": item.snapshot_product_code,
                    "product_name": item.snapshot_product_name,
                    "quantity": item.quantity,
                },
                entity_type="order_item",
                entity_id=item.id,
                object_ref=(
                    item.item_order_number
                    or f"{order.order_number}:{item.id}"
                ),
                resource="OrderItem",
                batch_id=batch_id,
            )
        _append_order_audit(
            db,
            request=request,
            user=user,
            order=order,
            action_code="order.delete",
            legacy_action="DELETE",
            description="删除无业务关联订单",
            details={
                "order_number": order.order_number,
                "customer_id": order.customer_id,
                "customer_po": order.customer_po,
                "item_count": len(order.items),
            },
            batch_id=batch_id,
        )
    if batch_id is not None:
        first_order = orders[0]
        customer = db.get(Customer, first_order.customer_id)
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="web",
            module_code="orders",
            action_code="order.group_delete",
            legacy_action="GROUP_DELETE",
            resource="OrderGroup",
            entity_type="order_group",
            object_ref=_order_group_key(first_order),
            customer_id=first_order.customer_id,
            customer_name=customer.name if customer is not None else None,
            batch_id=batch_id,
            description="批量删除同一客户订单组",
            details={
                "deleted_order_count": len(orders),
                "deleted_item_count": sum(
                    len(order.items) for order in orders
                ),
                "order_ids": [order.id for order in orders],
                "order_numbers": [
                    order.order_number for order in orders
                ],
            },
        )
    for order in orders:
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
    request: Request = None,
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
    remark = (payload.remark or "").strip() or None
    if target not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="订单状态无效")
    if target not in _MANUAL_ORDER_STATUS_TARGETS:
        raise HTTPException(
            status_code=409,
            detail=(
                "待报料、待收料、待生产、待送货、回单、对账、开票、结款和完成状态"
                "均由真实业务单据自动判断，不能手工修改。需要撤回流程时请使用受控撤回。"
            ),
        )
    if target in _MANUAL_ORDER_STATUS_TARGETS:
        _lock_orders_for_production_transition(db, [order.id])
        order = db.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise HTTPException(status_code=409, detail="订单已被删除，请刷新后重试")
    if order.status == target:
        customer = db.get(Customer, order.customer_id)
        return _order_response(
            order,
            user,
            db=db,
            customer_name=customer.name if customer else None,
        )
    if target in {"dead", "cancelled"}:
        _ensure_no_production_completion_facts(
            db,
            [item.id for item in order.items],
        )
        try:
            external_purchase_changes = cancel_unreceived_external_purchases(
                db,
                order_id=order.id,
                source=f"order_status_{target}",
                reason=remark or f"订单状态变更为{target}",
                cancelled_by=user.id,
            )
        except ExternalPackagingPurchaseLifecycleError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
    else:
        external_purchase_changes = []
        if target in {"closed", "archived"}:
            active_external_purchases = active_external_purchase_orders_for_order_ids(
                db, {order.id}
            )
            purchase_progress = purchase_receipt_progress(
                db,
                {int(purchase.id) for purchase in active_external_purchases},
            )
            unfinished_external_purchases = [
                purchase
                for purchase in active_external_purchases
                if purchase_progress.get(int(purchase.id), {}).get("status")
                != "received"
            ]
            if unfinished_external_purchases:
                numbers = "、".join(
                    purchase.purchase_number
                    for purchase in unfinished_external_purchases
                )
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单仍有未收齐的外购包材采购单 {numbers}，不能直接结档或归档；"
                        "请先完成收料，或将错误订单撤回后作废。"
                    ),
                )
    before = order.status
    order.status = target
    if remark is not None:
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
    _append_order_audit(
        db,
        request=request,
        user=user,
        order=order,
        action_code="order.status_change",
        legacy_action="STATUS",
        description=f"订单状态变更为{target}",
        details={
            "before": before,
            "after": target,
            "remark": remark,
            "external_packaging_purchase_changes": external_purchase_changes,
        },
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
    request: Request = None,
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
    _delete_orders_in_transaction(db, orders=[order], user=user, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/group-delete")
def delete_order_group(
    payload: OrderGroupDeleteRequest,
    request: Request = None,
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
    batch_id = uuid4().hex
    _delete_orders_in_transaction(
        db,
        orders=orders,
        user=user,
        request=request,
        batch_id=batch_id,
    )
    return {"deleted_count": len(orders), "batch_id": batch_id}


@router.put("/{order_id}/rollback-workflow")
def rollback_order_workflow(
    order_id: int,
    payload: WorkflowRollbackRequest,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_rollback),
) -> dict:
    reason = (payload.reason or "").strip() or "订单流程撤回（系统记录）"
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
    if order.status in MANAGEMENT_TERMINAL_ORDER_STATUSES:
        customer = db.get(Customer, order.customer_id)
        return _order_response(
            order,
            user,
            db=db,
            customer_name=customer.name if customer else None,
        )
    item_ids = [item.id for item in order.items]
    _ensure_no_production_completion_facts(db, item_ids)
    if _already_at_workflow_rollback_baseline(
        db,
        order=order,
        item_ids=item_ids,
    ):
        customer = db.get(Customer, order.customer_id)
        return _order_response(
            order,
            user,
            db=db,
            customer_name=customer.name if customer else None,
        )
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
    _ensure_statement_chain_can_be_removed_for_workflow_rollback(
        db,
        statement_ids,
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
        try:
            external_purchase_changes = cancel_unreceived_external_purchases(
                db,
                order_id=order.id,
                source="order_workflow_rollback",
                reason=reason,
                cancelled_by=user.id,
            )
        except ExternalPackagingPurchaseLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
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
        _append_order_audit(
            db,
            request=request,
            user=user,
            order=order,
            action_code="order.workflow_rollback",
            legacy_action="ROLLBACK_WORKFLOW",
            description="订单撤回到未送货未报料状态",
            details={
                "reason": reason,
                "delivery_ids": sorted(delivery_ids),
                "receipt_ids": receipt_ids,
                "statement_ids": sorted(statement_ids),
                "supplier_requisition_changes": supplier_requisition_changes,
                "external_packaging_purchase_changes": external_purchase_changes,
            },
        )
        db.commit()
        customer = db.get(Customer, order.customer_id)
        return _order_response(order, user, db=db, customer_name=customer.name if customer else None)
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        try:
            _ensure_statement_chain_can_be_removed_for_workflow_rollback(
                db,
                statement_ids,
            )
        except HTTPException as finance_conflict:
            raise finance_conflict from error
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/group-detail")
def get_order_group_detail(
    customer_id: int,
    anchor_order_id: int,
    customer_po: str | None = None,
    scope: Literal["active", "completed", "cancelled", "all"] = "active",
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return the complete, permission-checked customer + customer-PO group.

    Orders without a customer PO intentionally remain one-order groups so
    unrelated no-PO orders are never combined merely because the list page
    happens to show them together.
    """

    require_customer_access(customer_id, current_user=user, db=db)
    anchor = db.scalar(
        select(Order).where(
            Order.id == anchor_order_id,
            Order.customer_id == customer_id,
        )
    )
    if anchor is None:
        raise HTTPException(status_code=404, detail="订单汇总不存在，请刷新后重试")

    anchor_customer_po = (anchor.customer_po or "").strip()
    requested_customer_po = (customer_po or "").strip()
    if requested_customer_po:
        if anchor_customer_po != requested_customer_po:
            raise HTTPException(
                status_code=409,
                detail="订单汇总信息已变化，请刷新订单列表后重试",
            )
        group_condition = and_(
            Order.customer_id == customer_id,
            func.trim(Order.customer_po) == requested_customer_po,
        )
    else:
        if anchor_customer_po:
            raise HTTPException(
                status_code=409,
                detail="订单汇总信息已变化，请刷新订单列表后重试",
            )
        group_condition = Order.id == anchor.id

    candidate_orders = list(
        db.scalars(
            select(Order)
            .options(
                selectinload(Order.items)
                .selectinload(OrderItem.product)
                .selectinload(Product.drawings),  # type: ignore[attr-defined]
                selectinload(Order.items)
                .selectinload(OrderItem.product)
                .selectinload(Product.mold_tool),  # type: ignore[attr-defined]
            )
            .where(group_condition)
            .order_by(Order.created_at, Order.id)
        ).all()
    )
    if not candidate_orders:
        raise HTTPException(status_code=404, detail="订单汇总不存在，请刷新后重试")

    business_projections = build_order_business_statuses(
        db,
        candidate_orders,
        include_finance=has_permission(user, "finance.view"),
    )
    if scope == "cancelled":
        orders = [
            order
            for order in candidate_orders
            if order.status in _BUSINESS_EXCLUDED_STATUSES
        ]
    elif scope in {"active", "completed"}:
        orders = [
            order
            for order in candidate_orders
            if order.status not in _BUSINESS_EXCLUDED_STATUSES
            and (
                business_projections.get(int(order.id), {}).get("business_status")
                == "completed"
            )
            == (scope == "completed")
        ]
    else:
        orders = candidate_orders
    if anchor.id not in {order.id for order in orders}:
        raise HTTPException(
            status_code=409,
            detail="订单已不在当前业务范围，请刷新订单列表后重试",
        )

    full_response_context = _build_full_order_response_context(
        db,
        orders,
        user,
        business_projections=business_projections,
    )
    customer = db.get(Customer, customer_id)
    display_registry = build_display_registry(db)
    return {
        "customer_id": customer_id,
        "customer_name": customer.name if customer is not None else "-",
        "customer_po": requested_customer_po or None,
        "scope": scope,
        "orders": [
            _order_response(
                order,
                user,
                db=db,
                customer_name=customer.name if customer is not None else None,
                display_registry=display_registry,
                **_full_order_response_kwargs(
                    full_response_context, int(order.id)
                ),
            )
            for order in orders
        ],
    }


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
    full_response_context = _build_full_order_response_context(
        db,
        [order],
        user,
    )
    return _order_response(
        order,
        user,
        db=db,
        customer_name=customer.name if customer is not None else None,
        display_registry=display_registry,
        **_full_order_response_kwargs(full_response_context, int(order.id)),
    )


@router.get("/{order_id}/items/{item_id}/documents")
def get_order_item_documents(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return only real documents linked to one exact order detail."""

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    item = db.scalar(
        select(OrderItem).where(
            OrderItem.id == item_id,
            OrderItem.order_id == order.id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    customer = db.get(Customer, order.customer_id)
    display_registry = build_display_registry(db)
    return build_order_item_document_trace(
        db,
        order=order,
        item=item,
        customer_name=customer.name if customer is not None else "-",
        display_order_number=serialize_order_number_fields(
            order, display_registry
        )["display_order_number"],
        permissions={
            code
            for code in (
                "orders.view",
                "requisition.view",
                "incoming.view",
                "warehouse.view",
                "deliveries.view",
                "finance.view",
            )
            if has_permission(user, code)
        },
    )


@router.get(
    "/{order_id}/items/{item_id}/documents/{source_type}/{source_id}"
)
def get_order_item_document_detail(
    order_id: int,
    item_id: int,
    source_type: str,
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Revalidate and return one exact trace event without fuzzy lookup."""

    trace = get_order_item_documents(
        order_id=order_id,
        item_id=item_id,
        db=db,
        user=user,
    )
    event = next(
        (
            row
            for row in trace["events"]
            if row["source_type"] == source_type
            and row["source_id"] == source_id
        ),
        None,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="该阶段记录不属于当前订单明细")
    return {
        "order": trace["order"],
        "item": trace["item"],
        "navigation": {
            "order_id": order_id,
            "item_id": item_id,
            "source_type": source_type,
            "source_id": source_id,
        },
        "target": event["target"],
        "event": event,
    }


@router.put("/{order_id}")
def update_order(
    order_id: int,
    payload: OrderUpdate,
    request: Request = None,
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
    require_order_mold_repair_confirmation(
        db,
        product_ids=[item.product_id for item in order.items if item.product_id],
        confirmation_token=payload.mold_repair_confirmation_token,
        user=user,
    )

    before = {
        "customer_po": order.customer_po,
        "delivery_date": order.delivery_date,
        "remark": order.remark,
    }
    order.customer_po = (payload.customer_po or "").strip() or None
    order.delivery_date = payload.delivery_date
    order.remark = (payload.remark or "").strip() or None
    _append_order_audit(
        db,
        request=request,
        user=user,
        order=order,
        action_code="order.update",
        legacy_action="UPDATE",
        description="修改订单基础资料",
        details={
            "before": before,
            "after": {
                "customer_po": order.customer_po,
                "delivery_date": order.delivery_date,
                "remark": order.remark,
            },
        },
    )
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


_INACTIVE_REQUISITION_STATUSES = {
    "cancelled",
    "canceled",
    "voided",
    "withdrawn",
    "invalid",
    "已取消",
    "已作废",
    "已撤回",
}


def _component_has_active_requisition(db: Session, snapshot_id: int) -> bool:
    from app.models.raw_purchase_plan import RawPurchaseDemand
    from app.models.warehouse_inventory import OrderItemSemiRequirement
    if db.scalar(select(RawPurchaseDemand.id).join(OrderItemSemiRequirement,OrderItemSemiRequirement.id==RawPurchaseDemand.requirement_id)
            .where(OrderItemSemiRequirement.sales_order_item_bom_component_id==snapshot_id,RawPurchaseDemand.status=='active').limit(1)):
        return True
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
                func.lower(RequisitionItem.status).notin_(
                    _INACTIVE_REQUISITION_STATUSES
                ),
            )
            .limit(1)
        )
        is not None
    )


def _log_component_demand_change(
    db: Session,
    *,
    request: Request | None,
    user: User,
    order: Order,
    snapshot_id: int,
    before_quantity: int,
    after_quantity: int,
    idempotency_key: str,
) -> None:
    _append_order_audit(
        db,
        request=request,
        user=user,
        order=order,
        action_code="order.bom_component_demand.update",
        legacy_action="UPDATE_BOM_COMPONENT_DEMAND",
        description="修改本订单组件需求件数",
        details={
            "snapshot_id": snapshot_id,
            "before_required_piece_quantity": before_quantity,
            "after_required_piece_quantity": after_quantity,
            "idempotency_key": idempotency_key,
        },
        entity_type="sales_order_item_bom_component",
        entity_id=snapshot_id,
        object_ref=f"{order.order_number}:bom:{snapshot_id}",
        resource="OrderItem",
    )


def _apply_new_order_component_demands(
    db: Session,
    *,
    item: OrderItem,
    targets: list[NewOrderBomComponentDemand],
    user: User,
    order: Order,
    request: Request | None,
) -> None:
    if not targets:
        return
    preview = get_order_item_bom_preview(db, item.id)
    components = preview["components"]
    graph_components = any((row.get("snapshot_schema_version") or 0) >= 5 for row in components)
    by_relation_id = {
        int(component["product_bom_component_id"]): component
        for component in components
        if component.get("product_bom_component_id") is not None
    }
    relation_ids = [target.product_bom_component_id for target in targets]
    if len(relation_ids) != len(set(relation_ids)):
        raise HTTPException(status_code=400, detail="同一个订单组件不能重复填写数量")
    if any(relation_id not in by_relation_id for relation_id in relation_ids):
        raise HTTPException(
            status_code=409,
            detail="订单组件与当前常用箱 BOM 不一致，请刷新产品后重试",
        )
    for target in targets:
        component = by_relation_id[target.product_bom_component_id]
        snapshot_id = int(component["id"])
        current = int(component["effective_required_piece_quantity"])
        desired = target.required_piece_quantity
        if desired == current:
            continue
        if graph_components:
            raise HTTPException(status_code=409, detail="真实BOM组件数量须符合组套关系，请在常用箱维护配方或备料量")
        key = target.idempotency_key.strip()
        _adjustment, created = append_component_demand_adjustment(
            db,
            order_item_id=item.id,
            snapshot_id=snapshot_id,
            required_piece_quantity=desired,
            expected_required_piece_quantity=current,
            actor_id=user.id,
            idempotency_key=key,
        )
        if created:
            _log_component_demand_change(
                db,
                request=request,
                user=user,
                order=order,
                snapshot_id=snapshot_id,
                before_quantity=current,
                after_quantity=desired,
                idempotency_key=key,
            )
    if not graph_components:
        ensure_component_production_tasks(db, item.id)


def _validate_existing_component_demands(
    db: Session,
    *,
    item: OrderItem,
    targets: list[ExistingOrderBomComponentDemand],
) -> None:
    if not targets:
        return
    preview = get_order_item_bom_preview(db, item.id)
    current_by_snapshot = {
        int(component["id"]): int(component["effective_required_piece_quantity"])
        for component in preview["components"]
    }
    snapshot_ids = [target.snapshot_id for target in targets]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise HTTPException(status_code=400, detail="同一个订单组件不能重复填写数量")
    for target in targets:
        current = current_by_snapshot.get(target.snapshot_id)
        if current is None:
            raise HTTPException(status_code=404, detail="本订单组件快照不存在")
        if current != target.expected_required_piece_quantity:
            raise HTTPException(
                status_code=409,
                detail=(
                    "组件需求已由其他操作从"
                    f"{target.expected_required_piece_quantity}改为{current}，请刷新后重试"
                ),
            )
        if _component_has_active_requisition(db, target.snapshot_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "该组件已经正式报料，历史单据不能自动重算；"
                    "请先走受控撤销或新版本流程。"
                ),
            )


def _apply_existing_component_demands(
    db: Session,
    *,
    item: OrderItem,
    targets: list[ExistingOrderBomComponentDemand],
    user: User,
    order: Order,
    request: Request | None,
) -> None:
    if not targets:
        return
    preview = get_order_item_bom_preview(db, item.id)
    current_by_snapshot = {
        int(component["id"]): int(component["effective_required_piece_quantity"])
        for component in preview["components"]
    }
    for target in targets:
        current = current_by_snapshot[target.snapshot_id]
        desired = target.required_piece_quantity
        if desired == current:
            continue
        key = target.idempotency_key.strip()
        _adjustment, created = append_component_demand_adjustment(
            db,
            order_item_id=item.id,
            snapshot_id=target.snapshot_id,
            required_piece_quantity=desired,
            expected_required_piece_quantity=current,
            actor_id=user.id,
            idempotency_key=key,
        )
        if created:
            _log_component_demand_change(
                db,
                request=request,
                user=user,
                order=order,
                snapshot_id=target.snapshot_id,
                before_quantity=current,
                after_quantity=desired,
                idempotency_key=key,
            )
    ensure_component_production_tasks(db, item.id)


def _safe_order_save_log_text(
    value: object,
    *,
    max_length: int = 160,
) -> str | None:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:max_length] or None


def _safe_pdf_log_filename(value: object) -> str | None:
    normalized = str(value or "").replace("\\", "/")
    return _safe_order_save_log_text(Path(normalized).name, max_length=180)


def _set_order_save_stage(
    observability: dict[str, object] | None,
    stage: str,
) -> None:
    if observability is not None:
        observability["failure_stage"] = stage


def _rollback_order_drawing_consumptions(
    pending: list[PendingTemporaryConsumption],
    *,
    observability: dict[str, object] | None,
) -> None:
    for consumption in reversed(pending):
        errors = rollback_temporary_token_consumption(consumption)
        if errors:
            order_save_logger.error(
                "order_save_drawing_rollback_failed request_id=%s error_count=%s",
                (observability or {}).get("request_id"),
                len(errors),
            )


def _finalize_order_drawing_consumptions(
    pending: list[PendingTemporaryConsumption],
    *,
    observability: dict[str, object] | None,
) -> None:
    for consumption in pending:
        try:
            finalize_temporary_token_consumption(consumption)
        except OSError:
            # The database and permanent drawing are already committed.  Keep
            # the success response and let age-based cleanup remove the claim.
            order_save_logger.exception(
                "order_save_drawing_finalize_failed request_id=%s",
                (observability or {}).get("request_id"),
            )


def _create_wait_previous_batch_holds(
    db: Session, *, order: Order, items: list[OrderItem], payload_items: list[OrderItemCreate],
    selections: list[PreviousBatchSelection], user: User, request: Request | None
) -> None:
    """Create one fail-closed queue hold per eligible new line.

    A hold is intentionally only a pending-requisition queue gate; it never
    changes the existing order or requisition status fields.
    """
    selection_by_line = {row.client_line_id.strip(): row.previous_order_item_id for row in selections}
    if len(selection_by_line) != len(selections):
        raise HTTPException(status_code=422, detail="上一批选择不能重复")
    for index, (item, payload_item) in enumerate(zip(items, payload_items, strict=True), start=1):
        requested_previous_id = selection_by_line.get((payload_item.client_line_id or "").strip())
        if requested_previous_id == 0:
            append_audit_event(db, request=request, actor=user, event_category="business", result="success", source="web", module_code="requisition", action_code="requisition.hold.skip", legacy_action="SKIP_REQUISITION_HOLD", resource="Requisition", entity_type="order_item", entity_id=item.id, object_ref=item.item_order_number, customer_id=order.customer_id, description="新建订单明确正常待报料", details={"source": "order_create_strategy", "previous_order_item_id": 0})
            continue
        if active_finished_reserved_qty(db, item.id) >= int(item.quantity):
            continue
        code = (item.snapshot_product_code or "").strip()
        if not code:
            raise HTTPException(status_code=409, detail=f"第{index}条明细缺少存货编码，不能等待上一批")
        candidates = db.execute(
            select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id).where(
                Order.customer_id == order.customer_id,
                OrderItem.id != item.id,
                OrderItem.order_id != order.id,
                func.trim(OrderItem.snapshot_product_code) == code,
                OrderItem.delivered_quantity < OrderItem.quantity,
                OrderItem.is_force_closed.is_(False),
                Order.status.in_(ORDER_ITEM_ACTIVE_ORDER_STATUSES),
            ).order_by(OrderItem.created_at.desc(), OrderItem.id.desc()).limit(20)
        ).all()
        if not candidates:
            continue
        latest, latest_order = candidates[0]
        latest_is_held = db.scalar(select(RequisitionHold.id).where(
            RequisitionHold.order_item_id == latest.id, RequisitionHold.status == "active"
        ).limit(1)) is not None
        if latest_is_held and requested_previous_id not in (None, latest.id):
            raise HTTPException(status_code=409, detail=f"第{index}条明细必须等待最新等候批次，不能绕过链式依赖")
        if requested_previous_id is not None:
            selected = next((row for row in candidates if row[0].id == requested_previous_id), None)
            if selected is None:
                raise HTTPException(status_code=409, detail=f"第{index}条明细所选上一批不再可用，请重新预检")
            previous, previous_order = selected
        elif len(candidates) == 1:
            previous, previous_order = candidates[0]
        else:
            chained = db.scalar(select(RequisitionHold.id).where(
                RequisitionHold.order_item_id == latest.id, RequisitionHold.status == "active"
            ).limit(1))
            if chained is None:
                raise HTTPException(status_code=409, detail=f"第{index}条明细存在多个未送完的同款上一批，请选择要等待的批次")
            previous, previous_order = latest, latest_order
        mismatches = [
            label for label, left, right in (
                ("规格", previous.snapshot_spec, item.snapshot_spec),
                ("材质", previous.snapshot_material, item.snapshot_material),
                ("楞型", previous.flute_type, item.flute_type),
            ) if (left or "").strip() != (right or "").strip()
        ]
        if mismatches and requested_previous_id is None:
            raise HTTPException(status_code=409, detail=f"第{index}条明细与上一批快照{ '、'.join(mismatches) }不一致，不能自动等待")
        hold = RequisitionHold(
            order_item_id=item.id, order_item_id_snapshot=item.id,
            customer_id_snapshot=order.customer_id, customer_name_snapshot="",
            order_number_snapshot=order.order_number, order_item_sequence_snapshot=item.item_sequence,
            product_code_snapshot=code, product_name_snapshot=item.snapshot_product_name or "",
            specification_snapshot=resolved_product_specification(
                item.snapshot_spec,
                db.get(Product, item.product_id),
            ), quantity_snapshot=item.quantity,
            release_mode="previous_batch_completed", previous_order_item_id=previous.id,
            previous_order_item_id_snapshot=previous.id, status="active", version=1,
            created_by=user.id, updated_by=user.id,
        )
        customer = db.get(Customer, order.customer_id)
        hold.customer_name_snapshot = customer.name if customer is not None else ""
        db.add(hold)
        db.flush()
        append_audit_event(db, request=request, actor=user, event_category="business", result="success",
            source="web", module_code="requisition", action_code="requisition.hold.create",
            legacy_action="CREATE_REQUISITION_HOLD", resource="Requisition", entity_type="order_item",
            entity_id=item.id, object_ref=item.item_order_number, customer_id=order.customer_id,
            customer_name=hold.customer_name_snapshot, description="新建订单选择等待上一批送完后再报料",
            details={"source": "order_create_strategy", "strategy": "wait_previous_batch",
                     "previous_order_item_id": previous.id, "previous_order_number": previous_order.order_number,
                     "snapshot_warnings": mismatches, "selected_by_operator": requested_previous_id is not None})


def _create_order_impl(
    payload: OrderCreate,
    db: Session,
    user: User,
    *,
    commit: bool = True,
    source_contract_id: int | None = None,
    observability: dict[str, object] | None = None,
    request: Request | None = None,
):
    from app.models.multilevel_bom import ProductBomProfile
    from app.services.multilevel_bom_external_freeze import freeze_order_procurement
    from app.services.multilevel_bom_plan import BomPlanError
    pending_drawing_consumptions: list[PendingTemporaryConsumption] = []
    _set_order_save_stage(observability, "customer_scope")
    if payload.customer_id is not None:
        require_customer_access(payload.customer_id, current_user=user, db=db)
    if payload.items:
        require_order_mold_repair_confirmation(
            db,
            product_ids=[item.product_id for item in payload.items if item.product_id],
            confirmation_token=payload.mold_repair_confirmation_token,
            user=user,
        )
    if payload.items is None:
        _set_order_save_stage(observability, "legacy_create")
        return _legacy_create(payload, user)
    if not payload.items:
        raise HTTPException(status_code=400, detail="订单至少需要一条明细")
    if payload.customer_id is None:
        raise HTTPException(status_code=400, detail="客户不能为空")
    if not commit and any(item.temp_drawing_token for item in payload.items):
        raise HTTPException(
            status_code=400,
            detail="当前订单来源不支持临时图纸，请改用普通新建订单保存",
        )
    _set_order_save_stage(observability, "pdf_safety")
    pdf_safety_override_reasons, pdf_safety_claims = _validate_pdf_import_safety(
        payload,
        user,
        observability=observability,
    )
    if observability is not None and pdf_safety_claims:
        observability["pdf_filename"] = _safe_pdf_log_filename(
            pdf_safety_claims.get("source_name")
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
        from app.services.email_order_link import prepare as prepare_email_source, attach as attach_email_source
        from app.services.order_import_source import (
            attach as attach_import_source,
            audit_summary as import_source_audit_summary,
            prepare as prepare_import_source,
            replay_client_line_ids,
        )
        email_context, email_existing = prepare_email_source(db, payload, user, pdf_safety_claims)
        if email_existing is not None:
            existing_order = db.get(Order, email_existing.order_id)
            if existing_order is None:
                raise HTTPException(409, "邮件关联订单不存在，请核对来源记录")
            return _order_response(existing_order, user, db=db)
        import_source_context, existing_import_source = prepare_import_source(
            db,
            payload,
            user,
            pdf_safety_claims,
            email_context,
        )
        if existing_import_source is not None:
            existing_order = db.get(Order, existing_import_source.order_id)
            if existing_order is None:
                raise HTTPException(409, "PDF 来源关联订单不存在，请核对来源记录")
            response = _order_response(existing_order, user, db=db)
            line_ids = replay_client_line_ids(existing_import_source)
            for item in response.get("items", []):
                item["client_line_id"] = line_ids.get(item["id"])
            response["source_replay"] = True
            response["import_source_id"] = existing_import_source.id
            return response
        _set_order_save_stage(observability, "validate_customer")
        customer = db.get(Customer, payload.customer_id)
        if customer is None:
            raise HTTPException(status_code=400, detail="客户不存在")
        price_tax_terms = resolve_customer_price_tax_terms(db, customer.id)
        if payload.pdf_import_confirmation is not None and (
            not customer.is_active or customer.status != "active"
        ):
            raise HTTPException(status_code=400, detail="PDF 草稿所选客户已停用")

        customer_po = (payload.customer_po or "").strip() or None

        if payload.pdf_import_confirmation is not None:
            _set_order_save_stage(observability, "validate_pdf_master")
            from app.services.product_readiness import product_readiness
            master_issues = []
            for line_no, line in enumerate(payload.items, start=1):
                master = db.get(Product, line.product_id) if line.product_id and not line.is_new_product else None
                if master is not None and master.customer_id != customer.id:
                    raise HTTPException(400, f"第{line_no}行产品不属于当前客户")
                code = (master.product_code if master is not None else line.product_code) or "未识别编码"
                missing = product_readiness(master)["order_save_missing_labels"] if master is not None else ["未完成常用箱登记"]
                if missing:
                    master_issues.append(f"第{line_no}行【{code}】：{'、'.join(missing)}")
            if master_issues:
                raise HTTPException(400, "订单" + (customer_po or "识别草稿") + "无法保存：" + "；".join(master_issues) + "。请先编辑并保存对应常用箱，再返回保存订单。")

        new_product_cache: dict[str, Product] = {}
        resolved_products: dict[int, Product] = {}
        graph_modes: dict[int, str | None] = {}
        validated_quantities: dict[int, int] = {}
        validated_layer_flutes: dict[
            int,
            tuple[int | None, str | None, int | None, Material | None],
        ] = {}
        validated_external_purchase_ratios: dict[
            int,
            tuple[Decimal | None, Decimal | None, Decimal | None],
        ] = {}
        combination_provenances: dict[int, dict[str, object]] = {}
        _set_order_save_stage(observability, "validate_items")
        for index, item_payload in enumerate(payload.items, start=1):
            validated_quantities[index] = _validated_order_quantity(
                item_payload.quantity,
                index,
            )
            if item_payload.manual_size_entry:
                if item_payload.product_id is not None or item_payload.is_new_product:
                    raise HTTPException(
                        status_code=400,
                        detail=f"第{index}条手工尺寸订单不能同时选择已有常用箱或新产品标记",
                    )
                try:
                    product = resolve_or_create_manual_size_product(
                        db,
                        customer=customer,
                        data=ManualSizeProductInput(
                            client_line_id=(item_payload.client_line_id or "").strip(),
                            box_type=item_payload.box_type,
                            product_name=item_payload.product_name,
                            length_mm=item_payload.length_mm,
                            width_mm=item_payload.width_mm,
                            height_mm=item_payload.height_mm,
                            material_id=item_payload.material_id,
                            layer_count=item_payload.layer_count,
                            flute_type=item_payload.flute_type,
                            sale_unit_price=Decimal(str(item_payload.unit_price)),
                            report_length_mm=item_payload.report_length_mm,
                            report_width_mm=item_payload.report_width_mm,
                            crease_type=item_payload.crease_type,
                            crease_left_mm=item_payload.crease_left_mm,
                            crease_middle_mm=item_payload.crease_middle_mm,
                            crease_right_mm=item_payload.crease_right_mm,
                            base_report_length_mm=item_payload.base_report_length_mm,
                            base_report_width_mm=item_payload.base_report_width_mm,
                            base_crease_type=item_payload.base_crease_type,
                            base_crease_left_mm=item_payload.base_crease_left_mm,
                            base_crease_middle_mm=item_payload.base_crease_middle_mm,
                            base_crease_right_mm=item_payload.base_crease_right_mm,
                            splice_mode=item_payload.splice_mode,
                            pieces_per_box=item_payload.pieces_per_box,
                            flap_mm=item_payload.flap_mm,
                            default_cutting_mode=item_payload.default_cutting_mode,
                        ),
                        user=user,
                    )
                    # Preserve the exact manually entered dimensions as the
                    # order snapshot.  Do not route this through the legacy
                    # decimal display helper, which is only a product-read
                    # convenience and may shorten trailing integer zeroes.
                    item_payload.specification = "×".join(
                        str(value)
                        for value in (
                            item_payload.length_mm,
                            item_payload.width_mm,
                            item_payload.height_mm,
                        )
                        if value is not None
                    ) + "mm"
                except ManualSizeProductError as error:
                    raise HTTPException(
                        status_code=400, detail=f"第{index}条{error}"
                    ) from error
            elif item_payload.product_id is not None and not item_payload.is_new_product:
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
            validated_external_purchase_ratios[index] = (
                _validated_external_purchase_ratio(
                    item_payload,
                    product=product,
                    index=index,
                )
            )
            profile = db.get(ProductBomProfile, product.id) if product.id is not None else None
            graph_modes[index] = profile.source if profile else None
            if graph_modes[index] == "assembled" or bool(getattr(product, "is_virtual_composite_parent", False)):
                validated_layer_flutes[index] = (None, None, None, None)
                resolved_products[index] = product
                combination_provenances[index] = _validated_combination_provenance(
                    db,
                    customer=customer,
                    item_payload=item_payload,
                    product=product,
                    item_index=index,
                )
                continue

            crease_error = product_crease_width_error(product)
            if crease_error:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"第{index}条明细常用箱报料尺寸不一致：{crease_error}。"
                        "请先在常用箱中确认报料宽和压线尺寸后再下单"
                    ),
                )
            is_pdf_matched_product = bool(
                payload.pdf_import_confirmation is not None
                and item_payload.product_id is not None
                and not item_payload.is_new_product
            )
            selected_material_id = (
                None
                if product.supply_mode == "external_purchase"
                else (
                    product.material_id
                    if is_pdf_matched_product
                    else (
                        item_payload.material_id
                        if item_payload.material_id is not None
                        else product.material_id
                    )
                )
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
            if selected_material is not None and not selected_material.is_active:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细所选材质已停用，请更换启用材质",
                )
            if (
                selected_material is not None
                and (selected_material.supplier_name or "").strip()
            ):
                try:
                    resolve_supplier(
                        db,
                        selected_material.supplier_name,
                        require_active=True,
                    )
                except SupplierLookupError as error:
                    raise HTTPException(
                        status_code=400,
                        detail=f"第{index}条明细{error.message}",
                    ) from error
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
            combination_provenances[index] = _validated_combination_provenance(
                db,
                customer=customer,
                item_payload=item_payload,
                product=product,
                item_index=index,
            )

        _validate_combination_group_consistency(combination_provenances)
        _set_order_save_stage(observability, "inventory_preflight")
        reservation_plan_states = _preflight_reservation_plans(
            db,
            customer_id=customer.id,
            payload_items=payload.items,
            resolved_products=resolved_products,
        )

        related_orders: list[Order] = []
        if customer_po:
            _set_order_save_stage(observability, "duplicate_check")
            related_orders = db.scalars(
                select(Order)
                .options(selectinload(Order.items))
                .where(
                    Order.customer_id == customer.id,
                    Order.customer_po == customer_po,
                )
            ).all()
            if payload.pdf_import_confirmation is None:
                incoming_signature = sorted(
                    (
                        resolved_products[index].id,
                        validated_quantities[index],
                        str(Decimal(str(item.unit_price)).quantize(Decimal("0.0001"))),
                        (
                            resolved_product_specification(
                                item.specification,
                                resolved_products[index],
                            )
                            or ""
                        ),
                        str(validated_external_purchase_ratios[index][0] or ""),
                        str(validated_external_purchase_ratios[index][1] or ""),
                    )
                    for index, item in enumerate(payload.items, start=1)
                )
                for existing_order in related_orders:
                    existing_signature = sorted(
                        (
                            item.product_id,
                            item.quantity,
                            str(Decimal(str(item.unit_price)).quantize(Decimal("0.0001"))),
                            (item.snapshot_spec or "").strip(),
                            str(item.external_packaging_order_quantity_basis_snapshot or ""),
                            str(item.external_packaging_purchase_quantity_basis_snapshot or ""),
                        )
                        for item in existing_order.items
                    )
                    if existing_signature == incoming_signature:
                        raise HTTPException(
                            status_code=409,
                            detail="系统中已存在相同客户、客户单号和明细的订单，未重复生成。",
                        )

        _set_order_save_stage(observability, "persist_order")
        order_date = payload.order_date or beijing_today()
        order = Order(
            order_number=reserve_next_order_number(db, order_date),
            customer_id=customer.id,
            source_contract_id=source_contract_id,
            customer_po=customer_po,
            order_date=order_date,
            delivery_date=payload.delivery_date,
            status=payload.status,
            payment_status=payload.payment_status,
            requisition_strategy=payload.requisition_strategy,
            total_amount=Decimal("0"),
            remark=(payload.remark or "").strip() or None,
            created_by=user.id,
        )
        db.add(order)
        db.flush()

        total = Decimal("0")
        created_items: list[OrderItem] = []
        _set_order_save_stage(observability, "persist_items")
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
            (
                external_order_basis,
                external_purchase_basis,
                external_purchase_ratio,
            ) = validated_external_purchase_ratios[index]

            subtotal = (
                Decimal(quantity) * unit_price
            ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            total += subtotal
            item_sequence = reserve_next_item_sequence(db, order.id)
            # This is a second loop: do not reuse the last validated line's
            # PDF flag (assembled/virtual lines may skip that assignment).
            is_pdf_matched_product = bool(payload.pdf_import_confirmation is not None
                and item_payload.product_id is not None and not item_payload.is_new_product)
            initial_material_code = (
                (
                    selected_material.code
                    if selected_material is not None
                    else ((item_payload.material or "").strip() or product.legacy_material_text)
                )
                if is_pdf_matched_product
                else (
                    (item_payload.material or "").strip()
                    or (
                        selected_material.code
                        if selected_material is not None
                        else product.legacy_material_text
                    )
                )
            )
            if product.supply_mode == "external_purchase":
                initial_material_code = None
            original_material_code = (
                None
                if product.supply_mode == "external_purchase"
                else (
                    (item_payload.original_material_code or "").strip()
                    or (
                        (item_payload.material or "").strip()
                        if payload.pdf_import_confirmation is not None
                        else ""
                    )
                    or initial_material_code
                )
            )
            try:
                product_box_configuration = (
                    {
                        "splice_mode": None,
                        "pieces_per_box": None,
                        "flap_mm": None,
                        "default_cutting_mode": DEFAULT_CUTTING_MODE,
                    }
                    if graph_modes[index] == "assembled" or bool(
                        getattr(product, "is_virtual_composite_parent", False)
                    )
                    else _order_snapshot_box_configuration(product)
                )
            except BoxTypeRuleError as error:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"常用箱 {product.product_code} 的箱型配置无效："
                        f"{error}"
                    ),
                ) from error
            if not str(product.unit or '').strip():
                raise HTTPException(422, f'第{index + 1}行，存货编码 {product.product_code}：常用箱缺少销售单位，请完善后保存')
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
                price_tax_mode_snapshot=price_tax_terms.price_tax_mode,
                sales_unit_snapshot=(str(product.unit or '').strip() or None),
                tax_rate_snapshot=price_tax_terms.tax_rate,
                material_status="pending",
                snapshot_product_code=(
                    (item_payload.product_code or "").strip() or product.product_code
                ),
                snapshot_product_name=(
                    (item_payload.product_name or "").strip() or product.product_name
                ),
                snapshot_spec=resolved_product_specification(
                    item_payload.specification,
                    product,
                ),
                snapshot_material=initial_material_code,
                snapshot_original_material_code=original_material_code,
                snapshot_customer_model=(
                    (item_payload.customer_model or "").strip() or None
                ),  # v0.19.1: TH型号 / 客户型号
                snapshot_production_notes=(
                    None
                    if product.supply_mode == "external_purchase"
                    else (
                        (item_payload.production_notes or "").strip()
                        or (product.production_notes or "").strip()
                        or None
                    )
                ),  # v0.19.2-A: 生产/印刷说明
                supply_mode_snapshot=product.supply_mode,
                is_virtual_composite_parent_snapshot=bool(
                    getattr(product, "is_virtual_composite_parent", False)
                ),
                external_packaging_category_code_snapshot=(
                    product.external_packaging_category_code
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_specification_json_snapshot=(
                    product.external_packaging_specification_json
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_specification_summary_snapshot=(
                    product.external_packaging_specification_summary
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_purchase_unit_snapshot=(
                    product.external_packaging_purchase_unit
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_candidate_snapshot_json=(
                    product.external_packaging_candidate_snapshot_json
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_product_version_snapshot=(
                    int(product.version)
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_order_quantity_basis_snapshot=(
                    external_order_basis
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_purchase_quantity_basis_snapshot=(
                    external_purchase_basis
                    if product.supply_mode == "external_purchase"
                    else None
                ),
                external_packaging_quantity_per_finished_unit_snapshot=(
                    external_purchase_ratio
                    if product.supply_mode == "external_purchase"
                    else None
                ),
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
                snapshot_splice_mode=product_box_configuration["splice_mode"],
                snapshot_pieces_per_box=product_box_configuration[
                    "pieces_per_box"
                ],
                snapshot_flap_mm=product_box_configuration["flap_mm"],
                special_process=product_box_configuration[
                    "default_cutting_mode"
                ],
                requisition_status=(
                    "外购包材待确认"
                    if product.supply_mode == "external_purchase"
                    else "未报料"
                ),
                **combination_provenances[index],
            )
            # P0-B: the client can submit only a short-lived, owner-bound token.
            # A filesystem path is never interpreted from request data.
            if item_payload.temp_drawing_token:
                _set_order_save_stage(observability, "stage_drawing")
                try:
                    pending_drawing = stage_temporary_token_consumption(
                        item_payload.temp_drawing_token,
                        owner_id=user.id,
                        category="drawings",
                    )
                except UploadTokenError as error:
                    raise HTTPException(status_code=400, detail=str(error)) from error
                pending_drawing_consumptions.append(pending_drawing)
                item.drawing_file = pending_drawing.stored.reference
                _set_order_save_stage(observability, "persist_items")
            db.add(item)
            created_items.append(item)

        order.total_amount = total.quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
        db.flush()
        import_source = attach_import_source(
            db,
            import_source_context,
            order,
            created_items,
            payload.items,
            actor_id=user.id,
            override_reasons=pdf_safety_override_reasons,
        )
        _append_order_audit(
            db,
            request=request,
            user=user,
            order=order,
            action_code=(
                "order.pdf_create"
                if payload.pdf_import_confirmation is not None
                else "order.create"
            ),
            legacy_action="CREATE",
            source=(
                "import"
                if payload.pdf_import_confirmation is not None
                else "web"
            ),
            description=(
                "PDF 创建多明细订单"
                if payload.pdf_import_confirmation is not None
                else "创建多明细订单"
            ),
            details={
                "order_number": order.order_number,
                "customer_id": order.customer_id,
                "item_count": len(payload.items),
                "total_amount": str(order.total_amount),
                "source_contract_id": source_contract_id,
                "source_hash": pdf_safety_claims.get("source_hash") if pdf_safety_claims else None,
                "import_source_id": import_source.id if import_source is not None else None,
                "import_source_summary": import_source_audit_summary(db, import_source),
                "related_existing_order_ids": [row.id for row in related_orders],
            },
        )
        if pdf_safety_override_reasons:
            _append_order_audit(
                db,
                request=request,
                user=user,
                order=order,
                action_code="order.pdf_safety_override",
                legacy_action="PDF_SAFETY_OVERRIDE",
                source="import",
                description="人工明确确认后保存存在安全闸门状态的 PDF 草稿",
                details={
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
            )
        _set_order_save_stage(observability, "bom_and_production")
        db.flush()  # 获取 item.id 以便处理图纸
        for index, created_item in enumerate(created_items, start=1):
            product = resolved_products[index]
            if graph_modes[index] is not None:
                # Freeze graph and external-node identities together BEFORE the
                # legacy external writer can create unrelated parent-only rows.
                freeze_order_procurement(db, order_item_id=created_item.id, actor=user,
                                         root_order_snapshot=True)
                _apply_new_order_component_demands(db, item=created_item,
                    targets=payload.items[index - 1].bom_component_demands,
                    user=user, order=order, request=request)
                create_or_refresh_production_task(db, created_item.id)
                continue
            try:
                freeze_order_item_external_components(db, order_item=created_item)
            except OrderExternalPackagingSnapshotError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            if created_item.combination_role == "set_parent":
                create_order_item_bom_snapshots(
                    db,
                    order_item=created_item,
                    parent_product=product,
                )
                _apply_new_order_component_demands(
                    db,
                    item=created_item,
                    targets=payload.items[index - 1].bom_component_demands,
                    user=user,
                    order=order,
                    request=request,
                )
                if created_item.supply_mode_snapshot != "external_purchase":
                    create_or_refresh_production_task(db, created_item.id)
                continue
            if created_item.supply_mode_snapshot != "external_purchase":
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
        _set_order_save_stage(observability, "inventory_reservation")
        _apply_order_reservation_plans(
            db,
            order=order,
            payload_items=payload.items,
            created_items=created_items,
            resolved_products=resolved_products,
            states=reservation_plan_states,
            operator_id=user.id,
        )
        if payload.requisition_strategy == "wait_previous_batch":
            _create_wait_previous_batch_holds(
                db, order=order, items=created_items, payload_items=payload.items,
                selections=payload.previous_batch_selections, user=user, request=request
            )
        else:
            from app.services.bom_auto_reservation import reserve_new_order_stock
            for created_item in created_items:
                reserve_new_order_stock(db, order_item_id=created_item.id, operator_id=user.id)
        for index, created_item in enumerate(created_items, start=1):
            if created_item.combination_role != "set_parent":
                refresh_production_task(db, created_item.id)
        refresh_order_production_status(db, order.id)
        for created_item in created_items:
            material_snapshot, _ = freeze_order_item_material_cost(
                db,
                created_item,
                actor_id=user.id,
            )
            freeze_order_item_estimated_cost(
                db,
                created_item,
                material_snapshot=material_snapshot,
                actor_id=user.id,
            )
        attach_email_source(db, email_context, order, user)
        _set_order_save_stage(observability, "build_response")
        db.flush()
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
        if import_source is not None:
            response["source_replay"] = False
            response["import_source_id"] = import_source.id
        if related_orders:
            response["related_existing_orders"] = [
                {"id": row.id, "order_number": row.order_number, "status": row.status}
                for row in related_orders
            ]
        if payload.idempotency_key:
            from fastapi.encoders import jsonable_encoder
            key, digest = observability["create_identity"] if observability and "create_identity" in observability else _order_create_identity(payload, user.id)
            db.add(OperationLog(user_id=user.id, action="order_create_replay", resource="orders",
                request_id=key, entity_type="order", entity_id=order.id,
                details=json.dumps({"digest":digest,"response":jsonable_encoder(response)}, ensure_ascii=False),
                event_category="business", module_code="orders", action_code="create_replay_record", result="success"))
        if commit:
            _set_order_save_stage(observability, "commit")
            db.commit()
            _set_order_save_stage(observability, "finalize_drawing")
            _finalize_order_drawing_consumptions(
                pending_drawing_consumptions,
                observability=observability,
            )
        _set_order_save_stage(observability, "completed")
        return response
    except HTTPException:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise
    except CompositeBOMError as error:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise raise_composite_bom_http(error) from error
    except (CompositeBomWorkflowError, BomPlanError) as error:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProductionWorkflowError as error:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except IntegrityError as error:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise HTTPException(status_code=409, detail="订单号或订单数据冲突") from error
    except Exception:
        db.rollback()
        _rollback_order_drawing_consumptions(
            pending_drawing_consumptions,
            observability=observability,
        )
        raise


def _order_save_error_code(error: Exception) -> str | None:
    if not isinstance(error, HTTPException) or not isinstance(error.detail, dict):
        return None
    return _safe_order_save_log_text(error.detail.get("code"), max_length=80)


def _safe_order_save_exc_info(error: Exception) -> tuple[type[Exception], Exception, object]:
    safe_error = RuntimeError(type(error).__name__)
    return RuntimeError, safe_error, error.__traceback__


def _log_order_save_failure(
    *,
    error: Exception,
    observability: dict[str, object],
    status_code: int,
) -> None:
    order_save_logger.warning(
        (
            "order_save_failed request_id=%s actor_id=%s actor=%s "
            "pdf_filename=%s customer_id=%s customer_po=%s item_count=%s "
            "failure_stage=%s status=%s error_code=%s error_type=%s"
        ),
        observability.get("request_id"),
        observability.get("actor_id"),
        observability.get("actor"),
        observability.get("pdf_filename"),
        observability.get("customer_id"),
        observability.get("customer_po"),
        observability.get("item_count"),
        observability.get("failure_stage"),
        status_code,
        _order_save_error_code(error),
        type(error).__name__,
        exc_info=_safe_order_save_exc_info(error),
    )


@router.get("/create-attempts/{idempotency_key}")
def read_order_create_attempt(
    idempotency_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    """Resolve a browser's uncertain save without creating or refreshing an order."""
    if not 8 <= len(idempotency_key) <= 100:
        raise HTTPException(422, "保存请求标识无效")
    key = hashlib.sha256(f"order-create:{user.id}:{idempotency_key}".encode()).hexdigest()
    previous = db.scalar(select(OperationLog).where(
        OperationLog.request_id == key,
        OperationLog.action == "order_create_replay",
    ))
    if previous is None:
        # Absence is not permission to change identities: an original request
        # may still be in flight. The caller must retain the same key.
        return {"status": "not_found"}
    record = json.loads(previous.details)
    require_customer_access(record["response"]["customer_id"], current_user=user, db=db)
    order = db.get(Order, record["response"]["id"])
    if order is None:
        raise HTTPException(409, "原订单已不存在，请核对历史记录，不能重复创建。")
    return {"status": "completed", "order": {"id": order.id, "customer_id": order.customer_id}}


def _order_create_identity(payload: OrderCreate, actor_id: int):
    key = hashlib.sha256(f"order-create:{actor_id}:{payload.idempotency_key}".encode()).hexdigest()
    body = payload.model_dump(mode="json", exclude={"idempotency_key", "mold_repair_confirmation_token"})
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return key, digest


@router.post("", status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
):
    """Create a normal order and preserve the existing public API behavior.

    The delegated implementation still owns ``create_order_item_bom_snapshots``
    and ``create_or_refresh_production_task`` so normal orders and contractual
    orders follow the identical production workflow.
    """
    request_id = _safe_order_save_log_text(
        getattr(request.state, "request_id", None) if request is not None else None,
        max_length=80,
    )
    observability: dict[str, object] = {
        "request_id": request_id,
        "actor_id": user.id,
        "actor": _safe_order_save_log_text(user.username, max_length=80),
        "pdf_filename": None,
        "customer_id": payload.customer_id,
        "customer_po": _safe_order_save_log_text(
            payload.customer_po,
            max_length=120,
        ),
        "item_count": len(payload.items or []),
        "failure_stage": "entry",
    }
    try:
        if payload.idempotency_key or payload.pdf_import_confirmation is not None:
            connection = db.connection()
            if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            key, digest = _order_create_identity(payload, user.id)
            observability["create_identity"] = (key, digest)
            previous = db.scalar(select(OperationLog).where(
                OperationLog.request_id == key,
                OperationLog.action == "order_create_replay",
            ))
            if previous:
                record = json.loads(previous.details)
                require_customer_access(record["response"]["customer_id"], current_user=user, db=db)
                if record["digest"] != digest:
                    raise HTTPException(409, "此保存请求已完成，不能以同一请求标识提交不同内容；请核对原订单。")
                order = db.get(Order, record["response"]["id"])
                if order is None:
                    raise HTTPException(409, "原订单已不存在，请核对历史记录，不能重复创建。")
                response = _order_response(order, user, db=db)
                line_ids = {item["id"]: item.get("client_line_id") for item in record["response"].get("items", [])}
                for item in response.get("items", []):
                    item["client_line_id"] = line_ids.get(item["id"])
                db.rollback()
                return response
        return _create_order_impl(
            payload,
            db,
            user,
            commit=True,
            observability=observability,
            request=request,
        )
    except HTTPException as error:
        # Validation may run after an explicit manual-size common-box flush.
        # The product is part of this order transaction and must never survive
        # when a later line rejects the order.
        db.rollback()
        _log_order_save_failure(
            error=error,
            observability=observability,
            status_code=error.status_code,
        )
        raise
    except Exception as error:
        db.rollback()
        _log_order_save_failure(
            error=error,
            observability=observability,
            status_code=500,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "订单保存失败：服务器内部错误",
                "code": "ORDER_SAVE_INTERNAL_ERROR",
                "request_id": request_id,
            },
        ) from error


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


@router.put("/items/{item_id}/bom-components/{snapshot_id}/demand")
def update_order_item_bom_component_demand(
    item_id: int,
    snapshot_id: int,
    payload: BomComponentDemandUpdate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    if snapshot is None or snapshot.sales_order_item_id != item.id:
        raise HTTPException(status_code=404, detail="本订单组件快照不存在")

    if _component_has_active_requisition(db, snapshot.id):
        raise HTTPException(
            status_code=409,
            detail="该组件已经正式报料，历史单据不能自动重算；请先走受控撤销或新版本流程。",
        )

    key = payload.idempotency_key.strip()
    try:
        # Demand changes and component inventory reservations lock the same
        # order. This prevents a concurrent reduction from undercutting a
        # just-created finished/semi-finished coverage fact.
        lock_order_rows_for_production_transition(db, [order.id])
        if has_production_completion_facts(db, [item.id]) or int(
            item.delivered_quantity or 0
        ) > 0:
            raise HTTPException(
                status_code=409,
                detail="该订单明细已有生产或送货事实，组件需求不能直接修改；请走受控撤销或新版本流程。",
            )
        coverage = component_inventory_coverage(db, snapshot.id)
        if payload.required_piece_quantity < coverage["total_piece_quantity"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "组件需求不能低于已预占的成品/半成品覆盖数量"
                    f"（当前已覆盖 {coverage['total_piece_quantity']} 件）。"
                ),
            )
        _adjustment, created = append_component_demand_adjustment(
            db,
            order_item_id=item.id,
            snapshot_id=snapshot.id,
            required_piece_quantity=payload.required_piece_quantity,
            expected_required_piece_quantity=payload.expected_required_piece_quantity,
            actor_id=user.id,
            idempotency_key=key,
        )
        if not created:
            db.rollback()
            return get_order_item_bom_preview(db, item.id)
        ensure_component_production_tasks(db, item.id)
        freeze_order_item_material_cost(db, item, actor_id=user.id)
        material_snapshot = get_latest_order_item_material_cost_snapshot(db, item)
        freeze_order_item_estimated_cost(
            db,
            item,
            material_snapshot=material_snapshot,
            actor_id=user.id,
        )
        _log_component_demand_change(
            db,
            request=request,
            user=user,
            order=order,
            snapshot_id=snapshot.id,
            before_quantity=payload.expected_required_piece_quantity,
            after_quantity=payload.required_piece_quantity,
            idempotency_key=key,
        )
        db.commit()
        return get_order_item_bom_preview(db, item.id)
    except CompositeBomWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProductionWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="组件需求调整已提交，请刷新查看") from error


@router.post("/items/{item_id}/estimated-cost")
def update_order_item_estimated_cost(
    item_id: int,
    payload: EstimatedCostUpdate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_cost),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    order = db.get(Order, item.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(order.customer_id, current_user=user, db=db)
    latest = get_latest_order_item_estimated_cost_snapshot(db, item)
    if latest is None:
        raise HTTPException(status_code=409, detail="预计成本尚未冻结，请刷新订单后重试")
    if latest.snapshot_version != payload.expected_snapshot_version:
        raise HTTPException(
            status_code=409,
            detail="预计成本已被更新，请刷新后再修改",
        )
    try:
        material_snapshot, _ = freeze_order_item_material_cost(
            db, item, actor_id=user.id
        )
        snapshot, created = freeze_order_item_estimated_cost(
            db,
            item,
            material_snapshot=material_snapshot,
            actor_id=user.id,
            parameters=payload.model_dump(),
        )
        _append_order_audit(
            db,
            request=request,
            user=user,
            order=order,
            action_code="order.item.estimated_cost.update",
            legacy_action="UPDATE_ESTIMATED_COST",
            description="调整当前订单预计损耗与一次性费用",
            details={
                "snapshot_version": snapshot.snapshot_version,
                "created": created,
                "loss_rate": str(snapshot.loss_rate),
                "scope": "estimated_not_actual",
            },
            entity_type="order_item",
            entity_id=item.id,
            object_ref=item.item_order_number or str(item.id),
            resource="OrderItemEstimatedCost",
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if is_estimated_cost_snapshot_unique_conflict(error):
            raise HTTPException(
                status_code=409,
                detail="预计成本已被其他操作更新，请刷新后重试",
            ) from error
        raise
    return serialize_order_item_estimated_cost_snapshot(
        snapshot,
        sale_amount=item.subtotal,
    )


@router.put("/items/{item_id}")
def update_order_item(
    item_id: int,
    payload: OrderItemUpdate,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    from app.services.multilevel_bom_production_versions import order_production_values
    original_production_values = order_production_values(item)
    current_bom_production_revision = 0
    order_for_scope = db.get(Order, item.order_id)
    if order_for_scope is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    require_customer_access(
        order_for_scope.customer_id, current_user=user, db=db
    )
    _lock_orders_for_production_transition(db,[item.order_id])
    require_order_mold_repair_confirmation(
        db,
        product_ids=[item.product_id] if item.product_id else [],
        confirmation_token=payload.mold_repair_confirmation_token,
        user=user,
    )
    if payload.sync_product and not item.product_id:
        raise HTTPException(status_code=409, detail="当前订单明细未关联常用箱")
    material_reference_changed = bool(
        payload.material_id is not None
        and payload.material_id != item.material_id
    )
    if material_reference_changed:
        requested_material = db.get(Material, payload.material_id)
        if requested_material is None:
            raise HTTPException(status_code=400, detail="订单明细材质不存在")
        if not requested_material.is_active:
            raise HTTPException(status_code=400, detail="订单明细所选材质已停用")
        if (requested_material.supplier_name or "").strip():
            try:
                resolve_supplier(
                    db,
                    requested_material.supplier_name,
                    require_active=True,
                )
            except SupplierLookupError as error:
                raise HTTPException(
                    status_code=400,
                    detail=f"订单明细{error.message}",
                ) from error
    material_changed = bool(item.product_id and material_reference_changed)
    effective_sync_product = bool(
        item.product_id and (payload.sync_product or material_changed)
    )
    current_product = db.get(Product, item.product_id) if item.product_id else None
    requested_box_code = box_type_code(payload.box_style)
    current_box_code = box_type_code(current_product.box_style) if current_product else None
    requested_box_style = (payload.box_style or "").strip()
    current_box_style = (current_product.box_style or "").strip() if current_product else ""
    box_style_changed = (
        requested_box_code != current_box_code
        if requested_box_code is not None or current_box_code is not None
        else requested_box_style != current_box_style
    )
    if (
        current_product is not None
        and "box_style" in payload.model_fields_set
        and box_style_changed
        and not effective_sync_product
    ):
        raise HTTPException(
            status_code=400,
            detail="订单明细没有独立箱型字段；更改箱型时请勾选同步常用箱",
        )
    product_change_reason: str | None = None
    if effective_sync_product:
        if not has_permission(user, "products.edit"):
            _require_product_drawing_edit(user)
        if payload.product_expected_version is None:
            raise HTTPException(
                status_code=400,
                detail="同步常用箱必须提供 product_expected_version",
            )
        product_change_reason = (payload.product_change_reason or "").strip() or None
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
        if (
            payload.special_process is not None
            and payload.special_process != item.special_process
        ):
            sensitive_changes.append("开料方式")
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
        and selected_material_id != item.material_id
        and (selected_material.supplier_name or "").strip()
    ):
        try:
            resolve_supplier(
                db,
                selected_material.supplier_name,
                require_active=True,
            )
        except SupplierLookupError as error:
            raise HTTPException(
                status_code=400,
                detail=f"订单明细{error.message}",
            ) from error
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
    structure_fields = {
        "box_style",
        "snapshot_splice_mode",
        "snapshot_pieces_per_box",
        "snapshot_flap_mm",
        "snapshot_crease_type",
        "special_process",
    }
    structure_touched = bool(structure_fields.intersection(payload.model_fields_set))
    item_box_configuration: dict[str, object] | None = None
    structure_product = current_product
    if structure_touched:
        prospective_box_style = (
            payload.box_style
            if "box_style" in payload.model_fields_set
            else (structure_product.box_style if structure_product is not None else None)
        )
        try:
            item_box_configuration = normalize_box_configuration(
                box_style=prospective_box_style,
                splice_mode=(
                    payload.snapshot_splice_mode
                    if "snapshot_splice_mode" in payload.model_fields_set
                    else item.snapshot_splice_mode
                ),
                pieces_per_box=(
                    payload.snapshot_pieces_per_box
                    if "snapshot_pieces_per_box" in payload.model_fields_set
                    else item.snapshot_pieces_per_box
                ),
                flap_mm=(
                    payload.snapshot_flap_mm
                    if "snapshot_flap_mm" in payload.model_fields_set
                    else item.snapshot_flap_mm
                ),
                default_cutting_mode=(
                    payload.special_process
                    if "special_process" in payload.model_fields_set
                    else item.special_process
                ),
                crease_type=(
                    payload.snapshot_crease_type
                    if "snapshot_crease_type" in payload.model_fields_set
                    else item.snapshot_crease_type
                ),
            )
        except BoxTypeRuleError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    product_to_sync: Product | None = None
    prospective_product_layer: int | None = None
    prospective_product_flute: str | None = None
    if effective_sync_product and item.product_id:
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
    active_hold = db.scalar(
        select(RequisitionHold)
        .where(
            RequisitionHold.order_item_id == item.id,
            RequisitionHold.status == "active",
        )
        .limit(1)
    )
    if (
        active_hold is not None
        and payload.product_code.strip() != (item.snapshot_product_code or "").strip()
    ):
        raise HTTPException(
            status_code=409,
            detail="该明细正在等候报料，请先恢复待报料，再修改存货编码",
        )
    before = {
        "quantity": item.quantity,
        "unit_price": str(item.unit_price),
        "product_code": item.snapshot_product_code,
        "product_name": item.snapshot_product_name,
        "material": item.snapshot_material,
        "material_id": item.material_id,
        "supplier_name": item.snapshot_supplier_name,
        "specification": item.snapshot_spec,
        "special_process": item.special_process,
    }
    quantity_delta = int(payload.quantity) - int(item.quantity or 0)
    _validate_existing_component_demands(
        db,
        item=item,
        targets=payload.bom_component_demands,
    )
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
                reason="订单数量变更（系统记录）",
                actor_id=user.id,
                idempotency_key=adjustment_key,
                target_order_quantity=payload.quantity,
            )
        except CompositeBomWorkflowError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    item.quantity = payload.quantity
    _apply_existing_component_demands(
        db,
        item=item,
        targets=payload.bom_component_demands,
        user=user,
        order=order,
        request=request,
    )
    item.unit_price = unit_price
    item.subtotal = (Decimal(payload.quantity) * unit_price).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    item.snapshot_product_code = payload.product_code.strip()
    item.snapshot_product_name = payload.product_name.strip()
    item.snapshot_material = (
        selected_material.code
        if selected_material is not None
        else ((payload.material or "").strip() or None)
    )
    item.snapshot_spec = resolved_product_specification(
        payload.specification,
        current_product,
    )
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
    if item_box_configuration is not None:
        item.snapshot_splice_mode = str(item_box_configuration["splice_mode"])
        item.snapshot_pieces_per_box = int(item_box_configuration["pieces_per_box"])
        item.snapshot_flap_mm = item_box_configuration["flap_mm"]
        item.special_process = str(item_box_configuration["default_cutting_mode"])
        item.snapshot_crease_type = item_box_configuration["crease_type"]
        if item.snapshot_crease_type != "压线":
            item.snapshot_crease_left_mm = None
            item.snapshot_crease_middle_mm = None
            item.snapshot_crease_right_mm = None
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
            "length_mm",
            "width_mm",
            "height_mm",
            "production_process",
            "production_notes",
            "print_content",
        ):
            value = getattr(payload, field_name)
            if value is not None:
                add_product_update(field_name, value)
        if structure_touched:
            prospective_box_style = (
                payload.box_style
                if payload.box_style is not None
                else product.box_style
            )
            try:
                box_configuration = normalize_box_configuration(
                    box_style=prospective_box_style,
                    splice_mode=(
                        payload.snapshot_splice_mode
                        if payload.snapshot_splice_mode is not None
                        else product.splice_mode
                    ),
                    pieces_per_box=(
                        payload.snapshot_pieces_per_box
                        if payload.snapshot_pieces_per_box is not None
                        else product.pieces_per_box
                    ),
                    flap_mm=(
                        payload.snapshot_flap_mm
                        if payload.snapshot_flap_mm is not None
                        else product.flap_mm
                    ),
                    default_cutting_mode=(
                        payload.special_process
                        if "special_process" in payload.model_fields_set
                        else product.default_cutting_mode
                    ),
                    crease_type=(
                        payload.snapshot_crease_type
                        if payload.snapshot_crease_type is not None
                        else product.crease_type
                    ),
                )
            except BoxTypeRuleError as error:
                raise HTTPException(
                    status_code=400,
                    detail=str(error),
                ) from error
            if payload.box_style is not None:
                add_product_update(
                    "box_style",
                    box_configuration["box_style"],
                )
            add_product_update(
                "splice_mode",
                box_configuration["splice_mode"],
            )
            add_product_update(
                "pieces_per_box",
                box_configuration["pieces_per_box"],
            )
            add_product_update("flap_mm", box_configuration["flap_mm"])
            item.snapshot_splice_mode = box_configuration["splice_mode"]
            item.snapshot_pieces_per_box = box_configuration[
                "pieces_per_box"
            ]
            item.snapshot_flap_mm = box_configuration["flap_mm"]
            if payload.sync_product and "special_process" in payload.model_fields_set:
                add_product_update(
                    "default_cutting_mode",
                    box_configuration["default_cutting_mode"],
                )
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
            source=(
                "orders.update-item.sync-product:"
                f"{item.item_order_number or item.id}"
            ),
            confirmation_token=payload.product_confirmation_token,
        )
    from app.models.multilevel_bom import OrderBomGraph
    if db.get(OrderBomGraph, item.id) is not None:
        from app.services.multilevel_bom_orders import read_compiled_order_bom
        from app.services.multilevel_bom_production_versions import append_order_production_revision
        from app.services.multilevel_bom_plan import BomPlanError
        try:
            compiled = read_compiled_order_bom(db, item.id)
            current_bom_production_revision = max((getattr(row, "production_revision", 0) for row in compiled.snapshots), default=0)
            root = next(node for node in compiled.graph.nodes if node.product_id == item.product_id)
            changes = {field: value for field, value in order_production_values(item).items()
                       if value != original_production_values[field]}
            if changes and root.source == "manufactured":
                revision = append_order_production_revision(db, order_item_id=item.id,
                    changes={str(item.product_id): changes},
                    expected_revision=payload.bom_production_expected_revision, actor=user)
                current_bom_production_revision = revision.revision
        except BomPlanError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
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
    _append_order_audit(
        db,
        request=request,
        user=user,
        order=order,
        action_code="order.item.update",
        legacy_action="UPDATE",
        description="修改订单单条明细",
        details={
            "before": before,
            "after": {
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "subtotal": str(item.subtotal),
                "product_code": item.snapshot_product_code,
                "product_name": item.snapshot_product_name,
                "specification": item.snapshot_spec,
                "material": item.snapshot_material,
                "material_id": item.material_id,
                "supplier_name": item.snapshot_supplier_name,
                "layer_count": item.layer_count,
                "flute_type": item.flute_type,
                "production_notes": item.snapshot_production_notes,
                "special_process": item.special_process,
                "sync_product": effective_sync_product,
            },
            "source_reference": item.item_order_number or str(item.id),
        },
        entity_type="order_item",
        entity_id=item.id,
        object_ref=item.item_order_number or f"{order.order_number}:{item.id}",
        resource="OrderItem",
    )
    material_snapshot, _ = freeze_order_item_material_cost(
        db, item, actor_id=user.id
    )
    freeze_order_item_estimated_cost(
        db,
        item,
        material_snapshot=material_snapshot,
        actor_id=user.id,
    )
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "bom_production_revision": current_bom_production_revision,
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
        "snapshot_spec": resolved_product_specification(
            item.snapshot_spec,
            current_product,
        ),
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
        "special_process": item.special_process,
    }


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order_item(
    item_id: int,
    request: Request = None,
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
    cancelled_external = item.id in fully_cancelled_external_item_ids(db, [item.id])
    if item.requisition_status != "未报料" and not (
        cancelled_external and item.requisition_status == "外购包材已采购"
        and item.material_status == "pending"
    ):
        raise HTTPException(status_code=409, detail="请先取消报料再删除订单明细")
    external_purchase_history = db.scalar(
        select(ExternalPackagingPurchaseItem.id)
        .where(ExternalPackagingPurchaseItem.sales_order_item_id == item.id)
        .limit(1)
    )
    if external_purchase_history is not None and not cancelled_external:
        raise HTTPException(
            status_code=409,
            detail="该明细已有外购包材采购历史，不能删除；请保留原订单用于追溯。",
        )
    item_count = db.scalar(
        select(func.count()).select_from(OrderItem).where(
            OrderItem.order_id == order.id
        )
    )
    if item_count <= 1:
        raise HTTPException(status_code=409, detail="这是订单最后一条明细，请在更多操作中删除订单组")
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
    _invalidate_requisition_holds_before_item_delete(
        db,
        item_ids=[item.id],
        user=user,
        request=request,
    )
    if external_purchase_history is not None:
        _archive_cancelled_purchase_item_before_delete(db, order=order, item=item, user=user, request=request)
    db.delete(item)
    db.flush()
    _refresh_total(db, order)
    _append_order_audit(
        db,
        request=request,
        user=user,
        order=order,
        action_code="order.item.delete",
        legacy_action="DELETE",
        description="删除订单单条明细",
        details=deleted,
        entity_type="order_item",
        entity_id=item_id,
        object_ref=deleted["item_order_number"] or f"{order.order_number}:{item_id}",
        resource="OrderItem",
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _archive_cancelled_purchase_item_before_delete(db: Session, *, order: Order,
    item: OrderItem, user: User, request: Request | None) -> None:
    """Retain the complete cancelled line snapshot in audit under the DB purge gate.

    Never purge another order line, an active purchase, or any receipt history.
    Supplier headers and cancellation facts remain available for tracing.
    """
    rows = list(db.scalars(select(ExternalPackagingPurchaseItem).where(
        ExternalPackagingPurchaseItem.sales_order_item_id == item.id)))
    batch_ids = set(db.scalars(select(ExternalPackagingPurchaseOrder.batch_id).where(
        ExternalPackagingPurchaseOrder.id.in_({r.purchase_order_id for r in rows}))))
    purchases = list(db.scalars(select(ExternalPackagingPurchaseOrder).where(
        ExternalPackagingPurchaseOrder.batch_id.in_(batch_ids)).with_for_update()))
    purchase_ids = {p.id for p in purchases}
    cancelled = set(db.scalars(select(ExternalPackagingPurchaseCancellation.purchase_order_id).where(
        ExternalPackagingPurchaseCancellation.purchase_order_id.in_(purchase_ids))))
    if purchase_ids != cancelled or db.scalar(select(ExternalPackagingReceipt.id).where(
            ExternalPackagingReceipt.purchase_order_id.in_(purchase_ids)).limit(1)):
        raise HTTPException(409, "关联采购批次仍有有效采购或实收历史，请先撤回；有实收历史的明细请作废保留追溯")
    _append_order_audit(db, request=request, user=user, order=order,
        action_code="order.item.cancelled_purchase_archive", legacy_action="ARCHIVE_CANCELLED_EXT_ITEM",
        description="删除明细前归档已撤销且从未实收的外购采购行",
        details={"order_item_id": item.id, "purchase_items": [
            {c.name: str(getattr(row, c.name)) if getattr(row, c.name) is not None else None
             for c in ExternalPackagingPurchaseItem.__table__.columns} for row in rows]})
    for bid in batch_ids:
        db.add(ExternalPackagingPurchasePurgeAuthorization(batch_id=bid,
            authorized_by=user.id, reason="用户删除明细：已取消且从未实收，完整采购行已归档审计"))
    db.flush()
    db.execute(delete(ExternalPackagingPurchaseItem).where(
        ExternalPackagingPurchaseItem.id.in_([r.id for r in rows])))
    db.execute(delete(ExternalPackagingPurchasePurgeAuthorization).where(
        ExternalPackagingPurchasePurgeAuthorization.batch_id.in_(batch_ids)))
    db.flush()

# Explicit reserved-parts handoff, separate from ordinary order edits.
from app.api.bom_cutover import router as bom_cutover_router
router.include_router(bom_cutover_router)
from app.api.admin_order_reversal import router as admin_order_reversal_router
router.include_router(admin_order_reversal_router)
