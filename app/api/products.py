from __future__ import annotations

import json
import re
import mimetypes
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import (
    beijing_today,
    beijing_naive_to_api,
    beijing_now_naive,
    utc_naive_to_api,
)
from app.api.master_data_common import audit_master_change, clean_code
from app.models.customer import Customer
from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.material import Material
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.mold_tool import MoldTool
from app.models.printing_plate import PrintingPlate
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.supplier import ExternalPackagingProduct, Supplier
from app.models.user import User
from app.services.flute_mapping import (
    normalize_flute_type,
    seven_layer_code_error,
    validate_flute_for_write,
)
from app.services.product_drawings import (
    DrawingValidationError,
    remove_drawing_files,
    save_product_drawing_files,
)
from app.services.secure_uploads import (
    DRAWING_POLICY,
    UploadValidationError,
    read_validated_upload,
    resolve_stored_reference,
    stored_file_metadata,
)
from app.services.product_lifecycle import (
    has_historical_references,
)
from app.services.report_crease import crease_width_error
from app.services.product_readiness import product_readiness
from app.services.production_label_strategy import (
    ProductionLabelStrategyError,
    normalize_production_label_strategy,
)
from app.services.supplier_master import SupplierLookupError, resolve_supplier
from app.services.master_data_versioning import (
    apply_versioned_update,
    normalize_json_value,
    preview_versioned_update,
    record_versioned_create,
    serialize_versioned_entity,
)
from app.services.composite_bom import (
    CompositeBOMError,
    get_product_bom,
    order_selectable_product_condition,
    raise_http as raise_composite_bom_http,
    replace_product_bom,
)
from app.services.box_type_rules import (
    BOX_TYPE_RULES,
    BoxTypeRuleError,
    box_type_supports_cutting_mode,
    box_type_uses_flap,
    box_type_uses_splice,
    normalize_box_configuration,
    recommend_box_type,
)
from app.services.requisition_quantities import DEFAULT_CUTTING_MODE


router = APIRouter()
box_type_rules_router = APIRouter()
can_read = PermissionChecker("products.view")
can_create = PermissionChecker("products.create")
can_write = PermissionChecker("products.edit")
can_deactivate = PermissionChecker("products.deactivate")
admin_only = PermissionChecker("products.delete")
PRODUCT_DELETE_CONFLICT_DETAIL = (
    "该纸箱已在历史订单、报料或库存中使用，为了保证历史账目完整，"
    "系统禁止直接删除。请使用【停用】功能。"
)


def _box_style_uses_splice(box_style: str | None) -> bool:
    return box_type_uses_splice(box_style)


def _box_style_uses_tongue(box_style: str | None) -> bool:
    return box_type_uses_flap(box_style)


def _box_style_uses_default_cutting_mode(box_style: str | None) -> bool:
    return box_type_supports_cutting_mode(box_style)


def _production_process_tokens(value: str | None) -> set[str]:
    return {
        item.strip()
        for item in re.split(r"[,，、;；]", str(value or ""))
        if item.strip()
    }


_GLUE_PROCESS_TOKENS = {"粘合", "粘贴", "粘箱", "糊箱"}


def _production_process_uses_mold(value: str | None) -> bool:
    return "模切" in _production_process_tokens(value)


def _secondary_gluing_error(
    *,
    production_process: str | None,
    box_style: str | None,
    box_category: str | None,
) -> str | None:
    process_tokens = _production_process_tokens(production_process)
    if "二次粘合" not in process_tokens:
        return None
    if (
        box_category != "die_cut"
        or (box_style or "").strip() != "模切内盒"
        or "模切" not in process_tokens
        or not process_tokens.intersection(_GLUE_PROCESS_TOKENS)
    ):
        return (
            "二次粘合只允许用于模切内盒，箱类别必须为模切，"
            "且生产工艺必须同时包含模切和粘合"
        )
    return None


def _normalize_product_mold_binding(payload: ProductPayload) -> None:
    if _production_process_uses_mold(payload.production_process):
        if payload.mold_tool_id is None:
            raise HTTPException(
                status_code=400,
                detail="生产工艺包含模切，必须选择已登记的生产模具及货架位置",
            )
        return
    payload.mold_tool_id = None


_NO_PRINT_VALUES = {"", "无印刷", "无", "否", "不印刷"}


def _required_printing_plate_count(print_content: str | None) -> int:
    value = (print_content or "").strip()
    if value in _NO_PRINT_VALUES:
        return 0
    if value == "单色印刷":
        return 1
    if value == "双色印刷":
        return 2
    if value in {"三色印刷", "多色印刷"}:
        return 3
    return -1


def _normalize_product_printing_plate_configuration(payload: ProductPayload) -> None:
    required = _required_printing_plate_count(payload.print_content)
    plate_fields = (
        "printing_plate_1_id",
        "printing_plate_2_id",
        "printing_plate_3_id",
    )
    setting_fields = (
        "plate_alignment_value_mm",
        "plate_mount_value_mm",
        "machine_set_length_mm",
        "machine_set_width_mm",
        "machine_set_height_mm",
    )
    if required == 0 or payload.printing_plate_mode == "no_plate":
        payload.printing_plate_mode = "no_plate"
        for field in (*plate_fields, *setting_fields):
            setattr(payload, field, None)
        return
    if required < 0:
        raise ValueError("挂板印刷只支持单色、双色或三色印刷")
    ids = [getattr(payload, field) for field in plate_fields]
    selected = [value for value in ids if value is not None]
    if len(selected) != required or any(
        ids[index] is None for index in range(required)
    ) or any(ids[index] is not None for index in range(required, 3)):
        raise ValueError(f"{payload.print_content}挂板必须按颜色顺序选择 {required} 块挂板")
    if len(set(selected)) != len(selected):
        raise ValueError("同一块挂板不能在一个常用箱中重复绑定")


def _validate_product_printing_plates(db: Session, payload: ProductPayload) -> None:
    for plate_id in (
        payload.printing_plate_1_id,
        payload.printing_plate_2_id,
        payload.printing_plate_3_id,
    ):
        if plate_id is None:
            continue
        plate = db.get(PrintingPlate, plate_id)
        if plate is None:
            raise HTTPException(status_code=400, detail="所选挂板不存在")
        if plate.status != "active":
            raise HTTPException(status_code=400, detail=f"挂板 {plate.plate_code} 不是启用状态")
        if plate.customer_id != payload.customer_id:
            raise HTTPException(status_code=400, detail=f"挂板 {plate.plate_code} 不属于当前客户")


def _crease_width_error(
    *,
    label: str,
    crease_type: str | None,
    report_width_mm: int | None,
    left_mm: int | None,
    middle_mm: int | None,
    right_mm: int | None,
) -> str | None:
    """Compatibility wrapper for the shared write-time validator."""
    return crease_width_error(
        label=label,
        crease_type=crease_type,
        report_width_mm=report_width_mm,
        left_mm=left_mm,
        middle_mm=middle_mm,
        right_mm=right_mm,
    )


def _validate_product_crease_widths(payload: ProductPayload) -> None:
    errors = [
        _crease_width_error(
            label="压线",
            crease_type=payload.crease_type,
            report_width_mm=payload.report_width_mm,
            left_mm=payload.crease_left_mm,
            middle_mm=payload.crease_middle_mm,
            right_mm=payload.crease_right_mm,
        ),
        _crease_width_error(
            label="底压线",
            crease_type=payload.base_crease_type,
            report_width_mm=payload.base_report_width_mm,
            left_mm=payload.base_crease_left_mm,
            middle_mm=payload.base_crease_middle_mm,
            right_mm=payload.base_crease_right_mm,
        ),
    ]
    error = next((item for item in errors if item), None)
    if error:
        raise HTTPException(status_code=400, detail=error)


def _validate_changed_product_crease_widths(
    payload: ProductPayload,
    product: Product,
) -> None:
    def normalized(field_name: str, value: object) -> object:
        if field_name in {"crease_type", "base_crease_type"}:
            return value or None
        return value

    main_fields = {
        "report_length_mm",
        "report_width_mm",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
    }
    base_fields = {
        "base_report_length_mm",
        "base_report_width_mm",
        "base_crease_type",
        "base_crease_left_mm",
        "base_crease_middle_mm",
        "base_crease_right_mm",
    }

    def changed(field_names: set[str]) -> bool:
        return any(
            normalized(field_name, getattr(payload, field_name))
            != normalized(field_name, getattr(product, field_name))
            for field_name in field_names
        )

    if changed(main_fields):
        error = _crease_width_error(
            label="压线",
            crease_type=payload.crease_type,
            report_width_mm=payload.report_width_mm,
            left_mm=payload.crease_left_mm,
            middle_mm=payload.crease_middle_mm,
            right_mm=payload.crease_right_mm,
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
    if changed(base_fields):
        error = _crease_width_error(
            label="底压线",
            crease_type=payload.base_crease_type,
            report_width_mm=payload.base_report_width_mm,
            left_mm=payload.base_crease_left_mm,
            middle_mm=payload.base_crease_middle_mm,
            right_mm=payload.base_crease_right_mm,
        )
        if error:
            raise HTTPException(status_code=400, detail=error)


class ProductExternalSupplyCandidatePayload(BaseModel):
    external_product_id: int = Field(gt=0)
    is_default: bool = False


class ProductExternalSupplyPayload(BaseModel):
    candidates: list[ProductExternalSupplyCandidatePayload] = Field(default_factory=list, max_length=20)


class ProductPayload(BaseModel):
    customer_id: int
    product_code: str = Field(min_length=1, max_length=150)
    customer_material_code: str = Field(min_length=1, max_length=150)
    product_name: str = Field(min_length=1, max_length=250)
    material_id: int | None = None
    mold_tool_id: int | None = None
    legacy_material_text: str | None = None
    length_mm: int | None = Field(default=None, gt=0)
    width_mm: int | None = Field(default=None, gt=0)
    height_mm: int | None = Field(default=None, gt=0)
    box_category: str = Field(pattern="^(normal|die_cut)$")
    box_style: str | None = None
    supply_mode: Literal["corrugated_production", "external_purchase", "mixed_bom"] = "corrugated_production"
    external_packaging_category_code: str | None = None
    external_packaging_specification_summary: str | None = None
    external_packaging_purchase_unit: str | None = None
    external_supply: ProductExternalSupplyPayload | None = None
    print_content: str | None = None
    printing_colors: str | None = None
    printing_plate_mode: Literal["no_plate", "plate"] = "no_plate"
    printing_plate_1_id: int | None = Field(default=None, gt=0)
    printing_plate_2_id: int | None = Field(default=None, gt=0)
    printing_plate_3_id: int | None = Field(default=None, gt=0)
    plate_alignment_value_mm: Decimal | None = Field(default=None, ge=0)
    plate_mount_value_mm: Decimal | None = Field(default=None, ge=0)
    machine_set_length_mm: Decimal | None = Field(default=None, ge=0)
    machine_set_width_mm: Decimal | None = Field(default=None, ge=0)
    machine_set_height_mm: Decimal | None = Field(default=None, ge=0)
    production_process: str | None = None
    unit: str = "只"
    sale_unit_price: Decimal | None = Field(default=None, ge=0)
    sale_unit_price_no_tax: Decimal | None = Field(default=None, ge=0)
    cost_unit_price: Decimal | None = Field(default=None, ge=0)
    board_price: Decimal | None = Field(default=None, ge=0)
    suggested_price: Decimal | None = Field(default=None, ge=0)
    die_cut_path: str | None = None
    remark: str | None = None
    is_active: bool = True
    # Phase 17: 楞型字段
    flute_type: str | None = None
    layer_count: int | None = None
    surface_paper_type: str | None = None
    # v0.19.2-B: 报料尺寸 + 压线信息
    report_length_mm: int | None = None
    report_width_mm: int | None = None
    crease_type: str | None = Field(default=None, pattern="^(毛片|净料|压线|其他)$|^$")
    crease_left_mm: int | None = None
    crease_middle_mm: int | None = None
    crease_right_mm: int | None = None
    report_notes: str | None = None
    base_report_length_mm: int | None = None
    base_report_width_mm: int | None = None
    base_crease_type: str | None = Field(default=None, pattern="^(毛片|净料|压线|其他)$|^$")
    base_crease_left_mm: int | None = None
    base_crease_middle_mm: int | None = None
    base_crease_right_mm: int | None = None
    base_report_notes: str | None = None
    splice_mode: str | None = "single"
    pieces_per_box: int | None = None
    default_cutting_mode: str | int = DEFAULT_CUTTING_MODE
    production_label_enabled: bool = False
    production_label_units_per_label: int | None = Field(default=None, gt=0)
    flap_mm: int | None = 30
    combination_mode: Literal["parent_priced_set", "component_priced"] = "parent_priced_set"

    @model_validator(mode="after")
    def validate_flute_layer_consistency(self) -> "ProductPayload":
        """拒绝非法楞型/层数组合；七层写入必须明确 AAA/ABC。"""
        if self.supply_mode == "external_purchase":
            _clear_external_purchase_paper_fields(self)
        self.flute_type = normalize_flute_type(self.flute_type)
        # When a material is selected and layer_count is omitted, the endpoint
        # validates against Material.layer_count after loading the real row.
        err = (
            None
            if self.material_id is not None and self.layer_count is None
            else validate_flute_for_write(self.flute_type, self.layer_count)
        )
        if err:
            raise ValueError(err)
        configuration = normalize_box_configuration(
            box_style=self.box_style,
            splice_mode=self.splice_mode,
            pieces_per_box=self.pieces_per_box,
            flap_mm=self.flap_mm,
            default_cutting_mode=self.default_cutting_mode,
            crease_type=self.crease_type,
        )
        self.box_style = configuration["box_style"]
        self.splice_mode = configuration["splice_mode"]
        self.pieces_per_box = configuration["pieces_per_box"]
        self.flap_mm = configuration["flap_mm"]
        self.default_cutting_mode = configuration["default_cutting_mode"]
        if {
            "production_label_enabled",
            "production_label_units_per_label",
        }.intersection(self.model_fields_set):
            try:
                (
                    self.production_label_enabled,
                    self.production_label_units_per_label,
                ) = normalize_production_label_strategy(
                    box_style=self.box_style,
                    enabled=self.production_label_enabled,
                    units_per_label=self.production_label_units_per_label,
                )
            except ProductionLabelStrategyError as error:
                raise ValueError(str(error)) from error
        process_tokens = _production_process_tokens(self.production_process)
        if "印刷" in process_tokens:
            if (self.print_content or "").strip() in {
                "",
                "无印刷",
                "无",
                "否",
                "不印刷",
            }:
                self.print_content = "单色印刷"
            if not (self.printing_colors or "").strip():
                self.printing_colors = "黑色"
        _normalize_product_printing_plate_configuration(self)
        secondary_gluing_error = _secondary_gluing_error(
            production_process=self.production_process,
            box_style=self.box_style,
            box_category=self.box_category,
        )
        if secondary_gluing_error:
            raise ValueError(secondary_gluing_error)
        return self


class ProductDrawingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    image_path: str
    thumbnail_path: str
    uploaded_at: datetime


def _drawing_content_url(drawing: ProductDrawing, *, thumbnail: bool = False) -> str:
    reference = drawing.thumbnail_path if thumbnail else drawing.image_path
    suffix = Path(reference).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
        suffix = ".bin"
    variant = "thumbnail" if thumbnail else "original"
    return f"/api/master/products/drawings/{drawing.id}/content/{variant}{suffix}"


def _drawing_response(drawing: ProductDrawing) -> dict:
    return {
        "id": drawing.id,
        "image_path": _drawing_content_url(drawing),
        "thumbnail_path": _drawing_content_url(drawing, thumbnail=True),
        "uploaded_at": utc_naive_to_api(drawing.uploaded_at),
    }


class ProductResponse(ProductPayload):
    model_config = ConfigDict(from_attributes=True)

    id: int
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    deleted_at: datetime | None = None
    deleted_by: int | None = None
    purged_at: datetime | None = None
    version: int
    is_composite: bool = False
    combination_mode: Literal["parent_priced_set", "component_priced"] = "parent_priced_set"
    is_internal_component: bool = False
    drawings: list[ProductDrawingResponse] = Field(default_factory=list)


class ProductBOMComponentPayload(BaseModel):
    component_product_id: int = Field(gt=0)
    quantity_per_set: Decimal = Field(gt=0)
    is_die_cut: bool = False
    mold_tool_id: int | None = Field(default=None, gt=0)
    mold_max_yield_per_sheet: int | None = Field(default=None, gt=0)
    spare_sheet_quantity: int = Field(default=0, ge=0)
    display_mode: Literal[
        "internal_only", "show_on_delivery", "show_on_all_docs"
    ] = "internal_only"
    is_required: bool = True
    remark: str | None = Field(default=None, max_length=1000)

    @field_validator("quantity_per_set")
    @classmethod
    def validate_quantity_per_set_is_positive_integer(
        cls,
        value: Decimal,
    ) -> Decimal:
        if value != value.to_integral_value():
            raise ValueError("每套组件数量必须是正整数")
        return value

    @model_validator(mode="after")
    def validate_non_die_cut_mold_fields(self):
        if not self.is_die_cut and (
            self.mold_tool_id is not None
            or self.mold_max_yield_per_sheet is not None
        ):
            raise ValueError("非模切组件不能设置模具或模具最大产出")
        return self


class ProductBOMUpdatePayload(BaseModel):
    expected_version: int = Field(ge=1)
    change_reason: str | None = Field(default=None, max_length=500)
    components: list[ProductBOMComponentPayload] = Field(max_length=99)

    @field_validator("change_reason")
    @classmethod
    def validate_change_reason(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class ProductMutationPayload(BaseModel):
    expected_version: int = Field(ge=1)
    change_reason: str | None = Field(default=None, max_length=500)
    confirmation_token: str | None = None

    @field_validator("change_reason", mode="before")
    @classmethod
    def normalize_optional_change_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        reason = str(value).strip()
        return reason or None


class ProductUpdatePayload(ProductPayload):
    expected_version: int = Field(ge=1)
    change_reason: str | None = Field(default=None, max_length=500)
    confirmation_token: str | None = None

    @field_validator("change_reason", mode="before")
    @classmethod
    def normalize_optional_change_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        reason = str(value).strip()
        return reason or None


class ProductUpdatePreviewPayload(ProductPayload):
    expected_version: int = Field(ge=1)


class ProductStatusPayload(ProductMutationPayload):
    is_active: bool


class ProductTrashEmptyPayload(BaseModel):
    expected_versions: dict[int, int]
    change_reason: str | None = Field(default=None, max_length=500)
    confirmation_tokens: dict[int, str] = Field(default_factory=dict)

    @field_validator("expected_versions")
    @classmethod
    def validate_expected_versions(cls, value: dict[int, int]) -> dict[int, int]:
        if any(version < 1 for version in value.values()):
            raise ValueError("预期版本必须大于等于1")
        return value

    @field_validator("change_reason", mode="before")
    @classmethod
    def normalize_optional_change_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        reason = str(value).strip()
        return reason or None


PRICE_FIELDS = {
    "sale_unit_price",
    "sale_unit_price_no_tax",
    "cost_unit_price",
    "board_price",
    "suggested_price",
}
_COST_SENSITIVE_PRODUCT_FIELDS = frozenset(
    {"cost_unit_price", "board_price", "suggested_price"}
)
_PRODUCT_EXTERNAL_SUPPLY_FIELDS = frozenset({
    "supply_mode", "external_packaging_category_code",
    "external_packaging_specification_summary", "external_packaging_purchase_unit",
    "external_supply",
})
_PRODUCT_EXTERNAL_PROFILE_COLUMNS = (
    "supply_mode", "external_packaging_category_code",
    "external_packaging_specification_json", "external_packaging_specification_summary",
    "external_packaging_purchase_unit", "external_packaging_candidate_snapshot_json",
)
_EXTERNAL_PURCHASE_PAPER_FIELDS = (
    "material_id", "legacy_material_text", "length_mm", "width_mm", "height_mm",
    "flute_type", "layer_count", "surface_paper_type", "report_length_mm",
    "report_width_mm", "crease_type", "crease_left_mm", "crease_middle_mm",
    "crease_right_mm", "report_notes", "base_report_length_mm",
    "base_report_width_mm", "base_crease_type", "base_crease_left_mm",
    "base_crease_middle_mm", "base_crease_right_mm", "base_report_notes",
    "production_process", "printing_colors", "mold_tool_id", "die_cut_path",
)
_EXTERNAL_PURCHASE_SYNC_BLOCKED_FIELDS = frozenset(
    {*_EXTERNAL_PURCHASE_PAPER_FIELDS, "splice_mode", "pieces_per_box", "flap_mm", "box_style", "print_content"}
)


def _external_candidate_is_available(row: ExternalPackagingProduct) -> bool:
    if not row.is_active or row.supplier is None or not row.supplier.is_active:
        return False
    return any(
        item.is_active and item.category_code == row.category_code
        for item in row.supplier.supply_categories
    )


def _load_external_products(db: Session, ids: set[int]) -> dict[int, ExternalPackagingProduct]:
    if not ids:
        return {}
    rows = db.scalars(
        select(ExternalPackagingProduct)
        .options(
            selectinload(ExternalPackagingProduct.supplier).selectinload(Supplier.supply_categories),
            selectinload(ExternalPackagingProduct.customer_scope),
        )
        .where(ExternalPackagingProduct.id.in_(ids))
    ).all()
    return {row.id: row for row in rows}


def _external_supply_requested(payload: ProductPayload) -> bool:
    return bool(_PRODUCT_EXTERNAL_SUPPLY_FIELDS.intersection(payload.model_fields_set))


def _clear_external_purchase_paper_fields(payload: ProductPayload) -> None:
    for field in _EXTERNAL_PURCHASE_PAPER_FIELDS:
        setattr(payload, field, None)
    payload.splice_mode = "single"
    payload.pieces_per_box = 1
    payload.default_cutting_mode = DEFAULT_CUTTING_MODE
    payload.flap_mm = None
    payload.print_content = "无印刷"
    payload.printing_plate_mode = "no_plate"
    for field in (
        "printing_plate_1_id", "printing_plate_2_id", "printing_plate_3_id",
        "plate_alignment_value_mm", "plate_mount_value_mm", "machine_set_length_mm",
        "machine_set_width_mm", "machine_set_height_mm",
    ):
        setattr(payload, field, None)
    payload.production_label_enabled = False
    payload.production_label_units_per_label = None


def _normalize_product_external_supply(
    db: Session, *, payload: ProductPayload, existing: Product | None = None
) -> dict[str, object]:
    if existing is not None and not _external_supply_requested(payload):
        if existing.supply_mode == "external_purchase":
            _clear_external_purchase_paper_fields(payload)
        return {}
    if payload.supply_mode == "mixed_bom":
        if (payload.box_style or "").strip() == "其他":
            raise HTTPException(status_code=422, detail="混合 BOM 必须保留纸板主件，不能使用“其他”箱型")
        if payload.external_supply and payload.external_supply.candidates:
            raise HTTPException(status_code=422, detail="混合 BOM 的外购组件请在组件区维护，不能绑定纯外购候选")
        return {
            "supply_mode": "mixed_bom",
            "external_packaging_category_code": None,
            "external_packaging_specification_json": None,
            "external_packaging_specification_summary": None,
            "external_packaging_purchase_unit": None,
            "external_packaging_candidate_snapshot_json": None,
        }
    if payload.supply_mode != "external_purchase":
        if payload.external_supply and payload.external_supply.candidates:
            raise HTTPException(status_code=422, detail="纸板生产常用箱不能绑定外购包材候选")
        return {
            "supply_mode": "corrugated_production",
            "external_packaging_category_code": None,
            "external_packaging_specification_json": None,
            "external_packaging_specification_summary": None,
            "external_packaging_purchase_unit": None,
            "external_packaging_candidate_snapshot_json": None,
        }
    if (payload.box_style or "").strip() != "其他":
        raise HTTPException(status_code=422, detail="只有箱型选择“其他”才能使用外购包材供货")
    candidates = list(payload.external_supply.candidates if payload.external_supply else [])
    if not candidates:
        raise HTTPException(status_code=422, detail="当前外购包材没有候选供应商产品，不能保存")
    ids = [item.external_product_id for item in candidates]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="外购包材候选供应商产品不能重复")
    rows_by_id = _load_external_products(db, set(ids))
    if len(rows_by_id) != len(ids):
        raise HTTPException(status_code=422, detail="外购包材候选中包含不存在的供应商产品")
    rows = [rows_by_id[item_id] for item_id in ids]
    for row in rows:
        if not _external_candidate_is_available(row):
            raise HTTPException(status_code=409, detail=f"{row.supplier_product_code}或其供应商已停用")
        if row.customer_scope_id not in (None, payload.customer_id):
            raise HTTPException(status_code=409, detail=f"{row.supplier_product_code}是其他客户专用产品")
    first = rows[0]
    if any(
        row.category_code != first.category_code
        or row.specification_json != first.specification_json
        or row.purchase_unit != first.purchase_unit
        for row in rows[1:]
    ):
        raise HTTPException(status_code=422, detail="候选供应商必须属于同一包材类别、规格和采购单位")
    default_ids = [item.external_product_id for item in candidates if item.is_default]
    if len(candidates) == 1:
        default_ids = [candidates[0].external_product_id]
    if len(default_ids) != 1:
        raise HTTPException(status_code=422, detail="多个候选供应商时必须且只能明确一个默认供应商")
    snapshots = [
        {
            "external_product_id": row.id,
            "is_default": row.id == default_ids[0],
            "supplier_id": row.supplier_id,
            "supplier_name": row.supplier.display_name or row.supplier.standard_name,
            "supplier_product_code": row.supplier_product_code,
            "product_name": row.product_name,
            "purchase_unit": row.purchase_unit,
            "customer_scope_id": row.customer_scope_id,
            "external_product_version": row.version,
        }
        for row in rows
    ]
    _clear_external_purchase_paper_fields(payload)
    payload.external_packaging_category_code = first.category_code
    payload.external_packaging_specification_summary = first.specification_summary
    payload.external_packaging_purchase_unit = first.purchase_unit
    return {
        "supply_mode": "external_purchase",
        "external_packaging_category_code": first.category_code,
        "external_packaging_specification_json": first.specification_json,
        "external_packaging_specification_summary": first.specification_summary,
        "external_packaging_purchase_unit": first.purchase_unit,
        "external_packaging_candidate_snapshot_json": json.dumps(snapshots, ensure_ascii=False, sort_keys=True),
    }


def _external_supply_snapshot(product: Product) -> dict[str, object]:
    try:
        candidates = json.loads(product.external_packaging_candidate_snapshot_json or "[]")
    except (TypeError, json.JSONDecodeError):
        candidates = []
    try:
        specification = json.loads(product.external_packaging_specification_json or "{}")
    except (TypeError, json.JSONDecodeError):
        specification = {}
    return {
        "category_code": product.external_packaging_category_code,
        "specification": specification,
        "specification_summary": product.external_packaging_specification_summary,
        "purchase_unit": product.external_packaging_purchase_unit,
        "candidates": candidates,
    }


def _product_payload_snapshot(product: Product) -> dict:
    """Serialize stored product values without applying write-time validators.

    Historical products may contain incomplete or legacy layer/flute values.
    Reads must remain available so an operator can inspect and correct them;
    create/update requests still use ProductPayload and its strict validators.
    """
    data = {
        field_name: getattr(product, field_name, None)
        for field_name in ProductPayload.model_fields
        if field_name != "external_supply"
    }
    data["external_supply"] = _external_supply_snapshot(product)
    return data


def _product_write_data(payload: ProductPayload, user: User) -> dict:
    """Return the subset of product fields this user may persist.

    Sales staff may continue to create and edit ordinary product fields, but
    cost-sensitive values supplied by a client are never persisted without the
    cost.view permission.  On updates their stored values are consequently
    retained; on creates they keep the model defaults.
    """
    data = payload.model_dump(include=set(ProductPayload.model_fields))
    data.pop("external_supply", None)
    if not has_permission(user, "cost.view"):
        for field in _COST_SENSITIVE_PRODUCT_FIELDS:
            data.pop(field, None)
    return data


def _validated_product_versioned_updates(
    db: Session,
    *,
    product: Product,
    payload: ProductPayload,
    user: User,
) -> dict:
    supply_updates = _normalize_product_external_supply(db, payload=payload, existing=product)
    _normalize_product_mold_binding(payload)
    _normalize_product_printing_plate_configuration(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
        historical_material_id=product.material_id,
    )
    _validate_product_material_flute(db, payload)
    _validate_product_printing_plates(db, payload)
    _validate_changed_product_crease_widths(payload, product)
    updates = _product_write_data(payload, user)
    if (
        not _external_supply_requested(payload)
        and not {
        "production_label_enabled",
        "production_label_units_per_label",
        }.intersection(payload.model_fields_set)
    ):
        # Legacy full-update clients do not know these fields and must not
        # silently disable a strategy configured by a newer client.
        updates.pop("production_label_enabled", None)
        updates.pop("production_label_units_per_label", None)
    if not _external_supply_requested(payload):
        for field in _PRODUCT_EXTERNAL_PROFILE_COLUMNS:
            updates.pop(field, None)
    versioned_fields = set(serialize_versioned_entity("product", product))
    updates = {
        key: value for key, value in updates.items() if key in versioned_fields
    }
    updates.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
    )
    updates.update(supply_updates)
    return updates


def _product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="产品不存在")
    return product


def _response(product: Product, user: User) -> dict:
    data = {
        **_product_payload_snapshot(product),
        "id": product.id,
        "manual_modified": product.manual_modified,
        "manual_modified_at": (
            beijing_naive_to_api(product.manual_modified_at)
            if product.manual_modified_at
            else None
        ),
        "deleted_at": (
            beijing_naive_to_api(product.deleted_at) if product.deleted_at else None
        ),
        "deleted_by": product.deleted_by,
        "purged_at": (
            beijing_naive_to_api(product.purged_at) if product.purged_at else None
        ),
        "version": product.version,
        "is_composite": bool(getattr(product, "is_composite", False)),
        "is_internal_component": bool(
            getattr(product, "is_internal_component", False)
        ),
        "drawings": [
            _drawing_response(drawing)
            for drawing in product.drawings
        ],
    }
    # v0.19.2-B: 注入材质快照，供订单自动带出用
    if product.material is not None:
        m = product.material
        code = (m.code or "").split("-")[0].strip()
        data["material_code"] = code
        data["material_supplier_name"] = m.supplier_name
        data["material_weight"] = m.basis_weight_description
        data["material_flute_type"] = None
    else:
        data["material_code"] = None
        data["material_supplier_name"] = None
        data["material_weight"] = None
        data["material_flute_type"] = None
    data["readiness"] = product_readiness(product)
    if product.mold_tool is not None:
        data["mold_tool"] = {
            "id": product.mold_tool.id,
            "mold_code": product.mold_tool.mold_code,
            "mold_name": product.mold_tool.mold_name,
            "rack_location": product.mold_tool.rack_location,
            "is_active": product.mold_tool.is_active,
        }
    else:
        data["mold_tool"] = None
    data["printing_plates"] = [
        {
            "id": plate.id,
            "plate_code": plate.plate_code,
            "plate_name": plate.plate_name,
            "color_name": plate.color_name,
            "rack_location": plate.rack_location,
            "status": plate.status,
        }
        for plate in (
            product.printing_plate_1,
            product.printing_plate_2,
            product.printing_plate_3,
        )
        if plate is not None
    ]
    if product.deleted_at is not None:
        expires_at = product.deleted_at + timedelta(days=30)
        data["deleted_expires_at"] = beijing_naive_to_api(expires_at)
        data["trash_days_remaining"] = max(
            0,
            (expires_at - beijing_now_naive()).total_seconds() / 86400,
        )
    if not has_permission(user, "cost.view"):
        for field in {"cost_unit_price", "board_price", "suggested_price"}:
            data.pop(field, None)
    if user.role == "workshop":
        for field in PRICE_FIELDS:
            data.pop(field, None)
    return data


def _summary_response(product: Product, user: User) -> dict:
    """Return only fields used by the paginated common-box list."""
    material = product.material
    data = {
        "id": product.id,
        "customer_id": product.customer_id,
        "product_code": product.product_code,
        "customer_material_code": product.customer_material_code,
        "product_name": product.product_name,
        "supply_mode": product.supply_mode,
        "external_packaging_category_code": product.external_packaging_category_code,
        "external_packaging_specification_summary": product.external_packaging_specification_summary,
        "external_packaging_purchase_unit": product.external_packaging_purchase_unit,
        "length_mm": product.length_mm,
        "width_mm": product.width_mm,
        "height_mm": product.height_mm,
        "material_id": product.material_id,
        "legacy_material_text": product.legacy_material_text,
        "material_code": (
            (material.code or "").split("-")[0].strip() if material else None
        ),
        "material_supplier_name": material.supplier_name if material else None,
        "material_weight": material.basis_weight_description if material else None,
        "flute_type": product.flute_type,
        "sale_unit_price": product.sale_unit_price,
        "manual_modified": product.manual_modified,
        "version": product.version,
        "is_active": product.is_active,
        "readiness": product_readiness(product),
    }
    if user.role == "workshop":
        data.pop("sale_unit_price", None)
    return data


_PRODUCT_SYNC_DECIMAL_FIELDS = {
    "length_mm",
    "width_mm",
    "height_mm",
    "sale_unit_price",
    "cost_unit_price",
}
_PRODUCT_SYNC_INTEGER_FIELDS = {
    "material_id",
    "mold_tool_id",
    "layer_count",
    "report_length_mm",
    "report_width_mm",
    "crease_left_mm",
    "crease_middle_mm",
    "crease_right_mm",
    "base_report_length_mm",
    "base_report_width_mm",
    "base_crease_left_mm",
    "base_crease_middle_mm",
    "base_crease_right_mm",
    "pieces_per_box",
    "flap_mm",
}
_PRODUCT_SYNC_TEXT_FIELDS = {
    "remark",
    "report_notes",
    "production_process",
    "product_name",
    "base_crease_type",
    "base_report_notes",
    "crease_type",
    "flute_type",
    "splice_mode",
    "box_style",
    "print_content",
}


def _normalize_product_sync_field(field_name: str, value: object) -> object:
    if field_name in _PRODUCT_SYNC_DECIMAL_FIELDS:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            normalized = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError) as error:
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} 必须是有效数值",
            ) from error
        if not normalized.is_finite():
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} 必须是有限数值",
            )
        return normalized

    if field_name in _PRODUCT_SYNC_INTEGER_FIELDS:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            normalized = Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError) as error:
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} 必须是整数",
            ) from error
        if not normalized.is_finite() or normalized != normalized.to_integral_value():
            raise HTTPException(
                status_code=400,
                detail=f"{field_name} 必须是整数",
            )
        return int(normalized)

    if field_name in _PRODUCT_SYNC_TEXT_FIELDS:
        normalized = "" if value is None else str(value).strip()
        if field_name == "product_name" and not normalized:
            raise HTTPException(status_code=400, detail="产品名称不能为空")
        return normalized or None

    return value


def _changed_updates(product: Product, updates: dict) -> dict:
    return {
        key: value
        for key, value in updates.items()
        if normalize_json_value(getattr(product, key, None))
        != normalize_json_value(value)
    }


def _has_version_history(db: Session, product_id: int) -> bool:
    return db.scalar(
        select(MasterDataObjectVersion.id)
        .where(
            MasterDataObjectVersion.object_type == "product",
            MasterDataObjectVersion.object_id == product_id,
        )
        .limit(1)
    ) is not None


def _raise_product_version_conflict(product: Product, expected_version: int) -> None:
    if product.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MASTER_VERSION_CONFLICT",
                "expected_version": expected_version,
                "current_version": product.version,
            },
        )


def _cas_delete_product(
    db: Session,
    *,
    product_id: int,
    expected_version: int,
) -> None:
    version_history_exists = select(MasterDataObjectVersion.id).where(
        MasterDataObjectVersion.object_type == "product",
        MasterDataObjectVersion.object_id == product_id,
    ).exists()
    result = db.execute(
        delete(Product)
        .where(
            Product.id == product_id,
            Product.version == expected_version,
            ~version_history_exists,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1:
        return

    if _has_version_history(db, product_id):
        raise HTTPException(
            status_code=409,
            detail="该常用箱已有版本历史，禁止物理删除；请保留归档记录",
        )
    current_version = db.scalar(
        select(Product.version).where(Product.id == product_id)
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "MASTER_VERSION_CONFLICT",
            "expected_version": expected_version,
            "current_version": current_version,
        },
    )


def _validate_references(
    db: Session,
    *,
    customer_id: int,
    material_id: int | None,
    mold_tool_id: int | None,
    historical_material_id: int | None = None,
) -> None:
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    if material_id is not None:
        material = db.get(Material, material_id)
        if material is None:
            raise HTTPException(status_code=400, detail="材质不存在")
        # Editing an existing common box must stay possible when its historical
        # material link is unchanged.  A new link, however, is new business and
        # may only use a currently active material and supplier.
        if material_id != historical_material_id and not material.is_active:
            raise HTTPException(status_code=400, detail="所选材质已停用")
        if (
            material_id != historical_material_id
            and (material.supplier_name or "").strip()
        ):
            try:
                resolve_supplier(
                    db,
                    material.supplier_name,
                    require_active=True,
                )
            except SupplierLookupError as error:
                raise HTTPException(status_code=400, detail=error.message) from error
    if mold_tool_id is not None:
        mold_tool = db.get(MoldTool, mold_tool_id)
        if mold_tool is None:
            raise HTTPException(status_code=400, detail="模具不存在")
        if not mold_tool.is_active:
            raise HTTPException(status_code=400, detail="所选模具已停用")


def _validate_product_material_flute(db: Session, payload: ProductPayload) -> None:
    """Validate and normalize a product snapshot against its selected material."""
    material = db.get(Material, payload.material_id) if payload.material_id else None
    if (
        material is not None
        and payload.layer_count is not None
        and payload.layer_count != material.layer_count
    ):
        raise HTTPException(status_code=400, detail="请求层数与所选材质真实层数不一致")
    effective_layer_count = (
        material.layer_count if material is not None else payload.layer_count
    )
    code_error = seven_layer_code_error(
        material.code if material is not None else payload.legacy_material_text,
        effective_layer_count,
    )
    if code_error:
        raise HTTPException(status_code=400, detail=code_error)
    normalized_flute = normalize_flute_type(payload.flute_type)
    error = validate_flute_for_write(normalized_flute, effective_layer_count)
    if error:
        raise HTTPException(status_code=400, detail=error)
    payload.layer_count = effective_layer_count
    payload.flute_type = normalized_flute


class BoxTypeRecommendationPayload(BaseModel):
    box_style: str = Field(min_length=1, max_length=150)
    length_mm: int | None = Field(default=None, gt=0)
    width_mm: int | None = Field(default=None, gt=0)
    height_mm: int | None = Field(default=None, gt=0)
    splice_mode: Literal["single", "double"] = "single"
    flap_mm: int | None = Field(default=None, ge=0)
    crease_type: str | None = Field(default=None, max_length=20)


@router.get("/box-type-rules")
@box_type_rules_router.get("/box-type-rules")
def list_box_type_rules(
    _user: User = Depends(can_read),
) -> dict:
    """Stable read-only source for product forms and other UI consumers."""
    return {"rules": [rule.public_dict() for rule in BOX_TYPE_RULES]}


def _external_price_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(Decimal(value), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _current_external_candidate_prices(
    db: Session, rows: list[ExternalPackagingProduct]
) -> dict[int, ExternalPackagingPriceVersion]:
    if not rows:
        return {}
    product_by_id = {row.id: row for row in rows}
    as_of = beijing_today()
    prices = db.scalars(
        select(ExternalPackagingPriceVersion)
        .where(
            ExternalPackagingPriceVersion.external_product_id.in_(product_by_id),
            ExternalPackagingPriceVersion.effective_from <= as_of,
            or_(
                ExternalPackagingPriceVersion.effective_to.is_(None),
                ExternalPackagingPriceVersion.effective_to >= as_of,
            ),
        )
        .order_by(
            ExternalPackagingPriceVersion.external_product_id,
            ExternalPackagingPriceVersion.effective_from.desc(),
            ExternalPackagingPriceVersion.version_number.desc(),
        )
    ).all()
    current: dict[int, ExternalPackagingPriceVersion] = {}
    for price in prices:
        product = product_by_id.get(price.external_product_id)
        if product is None:
            continue
        if price.product_version != product.version or price.quote_unit != product.purchase_unit:
            continue
        current.setdefault(price.external_product_id, price)
    return current

@router.get("/external-supply-candidates")
def list_product_external_supply_candidates(
    customer_id: int = Query(gt=0),
    category_code: str = Query(min_length=1, max_length=50),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    rows = db.scalars(
        select(ExternalPackagingProduct)
        .options(selectinload(ExternalPackagingProduct.supplier).selectinload(Supplier.supply_categories))
        .where(
            ExternalPackagingProduct.category_code == category_code,
            ExternalPackagingProduct.is_active.is_(True),
            or_(ExternalPackagingProduct.customer_scope_id.is_(None), ExternalPackagingProduct.customer_scope_id == customer_id),
        )
        .order_by(ExternalPackagingProduct.supplier_id, ExternalPackagingProduct.supplier_product_code)
    ).all()
    available_rows = [row for row in rows if _external_candidate_is_available(row)]
    can_view_costs = has_permission(user, "cost.view")
    current_prices = (
        _current_external_candidate_prices(db, available_rows)
        if can_view_costs
        else {}
    )
    items = []
    for row in available_rows:
        item = {
            "external_product_id": row.id,
            "supplier_id": row.supplier_id,
            "supplier_name": row.supplier.display_name or row.supplier.standard_name,
            "supplier_product_code": row.supplier_product_code,
            "product_name": row.product_name,
            "category_code": row.category_code,
            "specification_summary": row.specification_summary,
            "specification": json.loads(row.specification_json or "{}"),
            "purchase_unit": row.purchase_unit,
            "customer_scope_id": row.customer_scope_id,
            "version": row.version,
        }
        if can_view_costs:
            price = current_prices.get(row.id)
            item["current_purchase_price"] = (
                {
                    "id": price.id,
                    "version_number": price.version_number,
                    "product_version": price.product_version,
                    "quote_unit": price.quote_unit,
                    "unit_price": _external_price_text(price.unit_price),
                    "currency": price.currency,
                    "tax_mode": price.tax_mode,
                    "tax_rate": _external_price_text(price.tax_rate),
                    "effective_from": price.effective_from.isoformat(),
                    "effective_to": price.effective_to.isoformat() if price.effective_to else None,
                    "evidence_reference": price.evidence_reference,
                }
                if price is not None
                else None
            )
        items.append(item)
    return {"customer_id": customer_id, "category_code": category_code, "items": items}


@router.post("/box-type-recommendation")
@box_type_rules_router.post("/box-type-recommendation")
def preview_box_type_recommendation(
    payload: BoxTypeRecommendationPayload,
    _user: User = Depends(can_read),
) -> dict:
    """Return confirmed suggestions without writing a product or database row."""
    try:
        return recommend_box_type(**payload.model_dump())
    except BoxTypeRuleError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("")
def list_products(
    customer_id: int | None = None,
    keyword: str = "",
    product_code: str = "",
    product_name: str = "",
    spec: str = "",
    material: str = "",
    include_inactive: bool = False,
    response_mode: Literal["full", "summary"] = Query(default="full"),
    selection_context: Literal["master_data", "order"] = Query(
        default="master_data"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = (
        select(Product)
        .outerjoin(Material, Material.id == Product.material_id)
        .where(Product.deleted_at.is_(None))
        .order_by(Product.customer_id, Product.product_code)
    )
    scoped_ids = customer_scope_ids(user, db)
    if not has_unrestricted_customer_access(user, db):
        query = query.where(Product.customer_id.in_(scoped_ids))
    if customer_id is not None:
        require_customer_access(customer_id, current_user=user, db=db)
        query = query.where(Product.customer_id == customer_id)
    if not include_inactive:
        query = query.where(Product.is_active.is_(True))
    if selection_context == "order":
        query = query.where(order_selectable_product_condition())
    for token in keyword.split():
        pattern = f"%{token}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.legacy_material_text.like(pattern),
                Material.code.like(pattern),
                Material.paper_composition.like(pattern),
                Material.flute_type.like(pattern),
                cast(Product.length_mm, String).like(pattern),
                cast(Product.width_mm, String).like(pattern),
                cast(Product.height_mm, String).like(pattern),
            )
        )
    if product_code.strip():
        query = query.where(Product.product_code.like(f"%{product_code.strip()}%"))
    if product_name.strip():
        query = query.where(Product.product_name.like(f"%{product_name.strip()}%"))
    if spec.strip():
        spec_numbers = re.findall(r"\d+(?:\.\d+)?", spec)
        if len(spec_numbers) >= 2:
            for column, value in zip(
                (Product.length_mm, Product.width_mm, Product.height_mm),
                spec_numbers[:3],
                strict=False,
            ):
                query = query.where(cast(column, String).like(f"%{value}%"))
        else:
            pattern = f"%{spec.strip()}%"
            query = query.where(
                or_(
                    cast(Product.length_mm, String).like(pattern),
                    cast(Product.width_mm, String).like(pattern),
                    cast(Product.height_mm, String).like(pattern),
                )
            )
    if material.strip():
        pattern = f"%{material.strip()}%"
        query = query.where(
            or_(
                Product.legacy_material_text.like(pattern),
                Material.code.like(pattern),
                Material.paper_composition.like(pattern),
                Material.flute_type.like(pattern),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    is_summary = response_mode == "summary"
    load_options = [selectinload(Product.material)]
    if not is_summary:
        load_options.extend(
            [
                selectinload(Product.drawings),
                selectinload(Product.mold_tool),
            ]
        )
    items = db.scalars(
        query.options(*load_options)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "items": [
            _summary_response(item, user)
            if is_summary
            else _response(item, user)
            for item in items
        ],
    }


@router.get("/trash")
def list_product_trash(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    query = (
        select(Product)
        .where(
            Product.deleted_at.is_not(None),
            Product.purged_at.is_(None),
        )
        .order_by(Product.deleted_at.desc(), Product.id.desc())
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_response(item, user) for item in items],
    }


@router.post("/trash/empty")
def empty_product_trash(
    payload: ProductTrashEmptyPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    products = db.scalars(
        select(Product).where(
            Product.deleted_at.is_not(None),
            Product.purged_at.is_(None),
        )
    ).all()
    for product in products:
        expected_version = payload.expected_versions.get(product.id)
        if expected_version is None:
            raise HTTPException(
                status_code=409,
                detail=f"常用箱 {product.product_code} 缺少预期版本，请刷新垃圾站后重试",
            )
        _raise_product_version_conflict(product, expected_version)
        if has_historical_references(db, product.id):
            raise HTTPException(
                status_code=409,
                detail="该常用箱已有业务历史引用，必须保留垃圾站记录",
            )
        if _has_version_history(db, product.id):
            raise HTTPException(
                status_code=409,
                detail="该常用箱已有版本历史，禁止物理删除；请保留归档记录",
            )
    deleted_count = 0
    archived_count = 0
    try:
        for product in products:
            details = {
                "customer_id": product.customer_id,
                "product_code": product.product_code,
            }
            _cas_delete_product(
                db,
                product_id=product.id,
                expected_version=payload.expected_versions[product.id],
            )
            deleted_count += 1
            action = "PURGE"
            audit_master_change(
                db,
                user=user,
                action=action,
                resource="Product",
                resource_id=product.id,
                details={
                    **details,
                    "reason": payload.change_reason
                    or "系统记录：清空常用箱垃圾站",
                },
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "deleted_count": deleted_count,
        "archived_count": archived_count,
    }


@router.get("/{product_id}")
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    return _response(product, user)


@router.post("/{product_id}/update-preview")
def preview_product_update(
    product_id: int,
    payload: ProductUpdatePreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    require_customer_access(payload.customer_id, current_user=user, db=db)
    updates = _validated_product_versioned_updates(
        db,
        product=product,
        payload=payload,
        user=user,
    )
    return preview_versioned_update(
        db,
        object_type="product",
        entity=product,
        updates=updates,
        expected_version=payload.expected_version,
        user=user,
    )


@router.get("/{product_id}/bom")
def read_product_bom(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    try:
        return get_product_bom(db, product_id)
    except CompositeBOMError as error:
        raise raise_composite_bom_http(error) from error


@router.put("/{product_id}/bom")
def update_product_bom(
    product_id: int,
    payload: ProductBOMUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    try:
        result = replace_product_bom(
            db,
            parent_product_id=product_id,
            components=[component.model_dump() for component in payload.components],
            expected_version=payload.expected_version,
            user=user,
            change_reason=payload.change_reason,
        )
        db.commit()
        return result
    except CompositeBOMError as error:
        db.rollback()
        raise raise_composite_bom_http(error) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="BOM 已被其他操作修改，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise


async def _create_drawing_version(
    product_id: int,
    file: UploadFile,
    db: Session,
    user: User,
) -> ProductDrawing:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    try:
        upload = await read_validated_upload(file, DRAWING_POLICY)
        saved = save_product_drawing_files(
            product_id=product.id,
            upload=upload,
        )
    except (DrawingValidationError, UploadValidationError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    drawing = ProductDrawing(
        product_id=product.id,
        image_path=saved.image_path,
        thumbnail_path=saved.thumbnail_path,
        uploaded_by=user.id,
    )
    db.add(drawing)
    audit_master_change(
        db,
        user=user,
        action="UPLOAD_DRAWING",
        resource="Product",
        resource_id=product.id,
        details={
            "original_filename": upload.original_filename,
            "content_type": upload.content_type,
            "size": upload.size,
            "sha256": upload.sha256,
        },
    )
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        remove_drawing_files(saved.image_path, saved.thumbnail_path)
        raise
    db.refresh(drawing)
    return drawing


@router.post("/{product_id}/drawings", status_code=status.HTTP_201_CREATED)
async def upload_product_drawing_version(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    drawing = await _create_drawing_version(product_id, file, db, user)
    return _drawing_response(drawing)


@router.post("/{product_id}/drawing")
async def upload_product_drawing(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    drawing = await _create_drawing_version(product_id, file, db, user)
    return {
        "id": drawing.id,
        "product_id": drawing.product_id,
        "drawing_path": _drawing_content_url(drawing),
        "image_path": _drawing_content_url(drawing),
        "thumbnail_path": _drawing_content_url(drawing, thumbnail=True),
    }


@router.get("/drawings/{drawing_id}/content/{display_name}")
def download_product_drawing(
    drawing_id: int,
    display_name: str,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> FileResponse:
    drawing = db.get(ProductDrawing, drawing_id)
    if drawing is None:
        raise HTTPException(status_code=404, detail="图纸版本不存在")
    product = _product_or_404(db, drawing.product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    # The requested variant is encoded in the final path component generated by
    # the server.  It never becomes a filesystem path.
    requested_name = str(display_name or "")
    use_thumbnail = requested_name.startswith("thumbnail")
    reference = drawing.thumbnail_path if use_thumbnail else drawing.image_path
    try:
        path = resolve_stored_reference(reference)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="图纸文件不存在") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图纸文件不存在")
    metadata = stored_file_metadata(path)
    content_type = str(
        metadata.get("content_type")
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    audit_master_change(
        db,
        user=user,
        action="VIEW_DRAWING",
        resource="Product",
        resource_id=product.id,
        details={"drawing_id": drawing.id, "thumbnail": use_thumbnail},
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


@router.delete("/drawings/{drawing_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product_drawing(
    drawing_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> Response:
    drawing = db.get(ProductDrawing, drawing_id)
    if drawing is None:
        raise HTTPException(status_code=404, detail="图纸版本不存在")
    product = _product_or_404(db, drawing.product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    image_path = drawing.image_path
    thumbnail_path = drawing.thumbnail_path
    audit_master_change(
        db,
        user=user,
        action="DELETE_DRAWING",
        resource="Product",
        resource_id=drawing.product_id,
        details={
            "drawing_id": drawing.id,
            "image_path": image_path,
            "thumbnail_path": thumbnail_path,
        },
    )
    db.delete(drawing)
    db.flush()
    db.commit()
    remove_drawing_files(image_path, thumbnail_path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    require_customer_access(payload.customer_id, current_user=user, db=db)
    supply_updates = _normalize_product_external_supply(db, payload=payload)
    _normalize_product_mold_binding(payload)
    _normalize_product_printing_plate_configuration(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
    )
    _validate_product_material_flute(db, payload)
    _validate_product_printing_plates(db, payload)
    _validate_product_crease_widths(payload)
    data = _product_write_data(payload, user)
    data.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
        manual_modified=True,
        manual_modified_at=beijing_now_naive(),
    )
    data.update(supply_updates)
    product = Product(**data)
    try:
        db.add(product)
        db.flush()
        record_versioned_create(
            db,
            object_type="product",
            entity=product,
            user=user,
            reason="新增常用箱",
            source="api.products.create",
        )
        audit_master_change(
            db,
            user=user,
            action="CREATE",
            resource="Product",
            resource_id=product.id,
            details=data,
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="同一客户下，存货编码与产品名称的组合不能重复",
        ) from error
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    return _response(product, user)


@router.put("/{product_id}")
def update_product(
    product_id: int,
    payload: ProductUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    require_customer_access(payload.customer_id, current_user=user, db=db)
    updates = _validated_product_versioned_updates(
        db,
        product=product,
        payload=payload,
        user=user,
    )
    try:
        revision = apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
            source="api.products.update",
            confirmation_token=payload.confirmation_token,
        )
        if revision is not None:
            product.manual_modified = True
            product.manual_modified_at = beijing_now_naive()
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="同一客户下，存货编码与产品名称的组合不能重复",
        ) from error
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    return _response(product, user)


@router.put("/{product_id}/status")
def update_product_status(
    product_id: int,
    payload: ProductStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_deactivate),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    if product.deleted_at is not None:
        raise HTTPException(status_code=400, detail="请先从垃圾站恢复该纸箱")
    before = product.is_active
    updates = {"is_active": payload.is_active}
    changed = _changed_updates(product, updates)
    try:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason
            or ("系统记录：启用常用箱" if payload.is_active else "系统记录：停用常用箱"),
            source="api.products.status",
            action="status_change",
            confirmation_token=payload.confirmation_token,
        )
        if changed:
            audit_master_change(
                db,
                user=user,
                action="ENABLE" if payload.is_active else "DISABLE",
                resource="Product",
                resource_id=product.id,
                details={"before": before, "after": payload.is_active},
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    return _response(product, user)


@router.put("/{product_id}/restore")
def restore_product(
    product_id: int,
    payload: ProductMutationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    if product.deleted_at is None:
        raise HTTPException(status_code=400, detail="该纸箱不在垃圾站中")
    if product.purged_at is not None:
        raise HTTPException(status_code=409, detail="该纸箱已永久归档，不能恢复")
    updates = {
        "is_active": True,
    }
    try:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason or "系统记录：从垃圾站恢复常用箱",
            source="api.products.restore",
            action="restore",
            confirmation_token=payload.confirmation_token,
        )
        product.deleted_at = None
        product.deleted_by = None
        product.purged_at = None
        audit_master_change(
            db,
            user=user,
            action="RESTORE",
            resource="Product",
            resource_id=product.id,
            details={"product_code": product.product_code},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    return _response(product, user)


@router.delete("/{product_id}/purge", response_model=None)
def purge_product(
    product_id: int,
    payload: ProductMutationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> Response | dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    if product.deleted_at is None:
        raise HTTPException(status_code=400, detail="请先将该纸箱移入垃圾站")
    details = {
        "customer_id": product.customer_id,
        "product_code": product.product_code,
    }
    _raise_product_version_conflict(product, payload.expected_version)
    if has_historical_references(db, product.id):
        raise HTTPException(
            status_code=409,
            detail="该常用箱已有业务历史引用，必须保留垃圾站记录",
        )
    if _has_version_history(db, product.id):
        raise HTTPException(
            status_code=409,
            detail="该常用箱已有版本历史，禁止物理删除；请保留归档记录",
        )
    try:
        audit_master_change(
            db,
            user=user,
            action="PURGE",
            resource="Product",
            resource_id=product.id,
            details={
                **details,
                "mode": "physical_delete",
                "reason": payload.change_reason or "系统记录：彻底清理常用箱",
            },
        )
        _cas_delete_product(
            db,
            product_id=product.id,
            expected_version=payload.expected_version,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class SyncFieldsPayload(BaseModel):
    """从订单明细同步部分字段回常用箱（用户确认后调用）。"""
    fields: dict  # e.g. {"layer_count": 3, "flute_type": "A", "material_id": 5, ...}
    expected_version: int = Field(ge=1)
    change_reason: str | None = Field(default=None, max_length=500)
    confirmation_token: str | None = None

    @field_validator("change_reason", mode="before")
    @classmethod
    def normalize_optional_change_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        reason = str(value).strip()
        return reason or None


@router.post("/{product_id}/sync-fields")
def sync_product_fields(
    product_id: int,
    payload: SyncFieldsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    """将订单录入时手动修改的字段同步回常用箱。只允许同步白名单字段。"""
    ALLOWED = {
        "layer_count", "flute_type", "material_id",
        "length_mm", "width_mm", "height_mm",
        "sale_unit_price", "cost_unit_price", "remark",
        # v0.19.2-B: 报料尺寸 + 压线同步
        "report_length_mm", "report_width_mm",
        "crease_type", "crease_left_mm", "crease_middle_mm", "crease_right_mm",
        "report_notes", "production_process", "product_name", "specification",
        "base_report_length_mm", "base_report_width_mm",
        "base_crease_type", "base_crease_left_mm", "base_crease_middle_mm",
        "base_crease_right_mm", "base_report_notes",
        "splice_mode", "pieces_per_box", "flap_mm", "box_style", "print_content",
        "mold_tool_id",
    }
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    _raise_product_version_conflict(product, payload.expected_version)
    if not has_permission(user, "cost.view"):
        for field in _COST_SENSITIVE_PRODUCT_FIELDS:
            payload.fields.pop(field, None)
    versioned_fields = set(serialize_versioned_entity("product", product))
    fields = {
        field_name: _normalize_product_sync_field(field_name, value)
        for field_name, value in payload.fields.items()
        if field_name in ALLOWED
        and field_name in Product.__table__.columns
        and field_name in versioned_fields
    }
    if product.supply_mode == "external_purchase":
        blocked = sorted(_EXTERNAL_PURCHASE_SYNC_BLOCKED_FIELDS.intersection(fields))
        if blocked:
            raise HTTPException(
                status_code=409,
                detail="外购包材常用箱不能从订单同步纸板材质、尺寸、报料或生产字段",
            )
    if not fields:
        raise HTTPException(status_code=400, detail="没有可同步的字段")
    changed = _changed_updates(product, fields)
    if not changed:
        return {
            "updated": [],
            "product_id": product.id,
            "version": product.version,
        }

    requested_layer = fields.get("layer_count", product.layer_count)
    requested_flute = normalize_flute_type(
        fields.get("flute_type", product.flute_type)
    )
    if "layer_count" in fields:
        requested_error = validate_flute_for_write(requested_flute, requested_layer)
        if requested_error:
            raise HTTPException(status_code=400, detail=requested_error)
    selected_material_id = fields.get("material_id", product.material_id)
    selected_material = (
        db.get(Material, selected_material_id)
        if selected_material_id is not None
        else None
    )
    if selected_material_id is not None and selected_material is None:
        raise HTTPException(status_code=400, detail="材质不存在")
    if (
        selected_material is not None
        and selected_material_id != product.material_id
        and not selected_material.is_active
    ):
        raise HTTPException(status_code=400, detail="所选材质已停用")
    if (
        selected_material is not None
        and selected_material_id != product.material_id
        and (selected_material.supplier_name or "").strip()
    ):
        try:
            resolve_supplier(
                db,
                selected_material.supplier_name,
                require_active=True,
            )
        except SupplierLookupError as error:
            raise HTTPException(status_code=400, detail=error.message) from error
    if (
        selected_material is not None
        and "layer_count" in fields
        and fields["layer_count"] is not None
        and fields["layer_count"] != selected_material.layer_count
    ):
        raise HTTPException(status_code=400, detail="请求层数与所选材质真实层数不一致")
    prospective_layer = (
        selected_material.layer_count
        if selected_material is not None
        else fields.get("layer_count", product.layer_count)
    )
    code_error = seven_layer_code_error(
        selected_material.code
        if selected_material is not None
        else product.legacy_material_text,
        prospective_layer,
    )
    if code_error:
        raise HTTPException(status_code=400, detail=code_error)
    prospective_flute = normalize_flute_type(
        fields.get("flute_type", product.flute_type)
    )
    flute_fields = {"material_id", "layer_count", "flute_type"}
    flute_error = validate_flute_for_write(prospective_flute, prospective_layer)
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)
    if selected_material is not None and flute_fields.intersection(fields):
        fields["layer_count"] = prospective_layer
    if {"production_process", "box_style"}.intersection(fields):
        secondary_gluing_error = _secondary_gluing_error(
            production_process=fields.get(
                "production_process", product.production_process
            ),
            box_style=fields.get("box_style", product.box_style),
            box_category=product.box_category,
        )
        if secondary_gluing_error:
            raise HTTPException(status_code=400, detail=secondary_gluing_error)
    if "production_process" in fields or "mold_tool_id" in fields:
        prospective_process = fields.get(
            "production_process", product.production_process
        )
        prospective_mold_id = fields.get(
            "mold_tool_id", product.mold_tool_id
        )
        if _production_process_uses_mold(prospective_process):
            if prospective_mold_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="生产工艺包含模切，必须选择已登记的生产模具及货架位置",
                )
            mold_tool = db.get(MoldTool, prospective_mold_id)
            if mold_tool is None:
                raise HTTPException(status_code=400, detail="模具不存在")
            if not mold_tool.is_active:
                raise HTTPException(status_code=400, detail="所选模具已停用")
        else:
            fields["mold_tool_id"] = None
    main_crease_fields = {
        "report_width_mm",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
    }
    if main_crease_fields.intersection(fields):
        error = _crease_width_error(
            label="压线",
            crease_type=fields.get("crease_type", product.crease_type),
            report_width_mm=fields.get(
                "report_width_mm", product.report_width_mm
            ),
            left_mm=fields.get("crease_left_mm", product.crease_left_mm),
            middle_mm=fields.get(
                "crease_middle_mm", product.crease_middle_mm
            ),
            right_mm=fields.get(
                "crease_right_mm", product.crease_right_mm
            ),
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
    base_crease_fields = {
        "base_report_width_mm",
        "base_crease_type",
        "base_crease_left_mm",
        "base_crease_middle_mm",
        "base_crease_right_mm",
    }
    if base_crease_fields.intersection(fields):
        error = _crease_width_error(
            label="底压线",
            crease_type=fields.get(
                "base_crease_type", product.base_crease_type
            ),
            report_width_mm=fields.get(
                "base_report_width_mm", product.base_report_width_mm
            ),
            left_mm=fields.get(
                "base_crease_left_mm", product.base_crease_left_mm
            ),
            middle_mm=fields.get(
                "base_crease_middle_mm", product.base_crease_middle_mm
            ),
            right_mm=fields.get(
                "base_crease_right_mm", product.base_crease_right_mm
            ),
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
    updates = {}
    for k, v in fields.items():
        if k == "flute_type":
            v = prospective_flute
        updates[k] = v
    changed = _changed_updates(product, updates)
    if not changed:
        return {
            "updated": [],
            "product_id": product.id,
            "version": product.version,
        }
    try:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=changed,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason or "系统记录：订单字段同步到常用箱",
            source="api.products.sync_fields",
            action="sync_fields",
            confirmation_token=payload.confirmation_token,
        )
        if changed:
            audit_master_change(
                db,
                user=user,
                action="SYNC_FIELDS_FROM_ORDER",
                resource="Product",
                resource_id=product.id,
                details={"synced": sorted(changed)},
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    return {
        "updated": sorted(changed),
        "product_id": product.id,
        "version": product.version,
    }


@router.delete("/{product_id}")
def delete_product(
    product_id: int,
    payload: ProductMutationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    if product.deleted_at is not None:
        raise HTTPException(status_code=400, detail="该纸箱已在垃圾站中")
    updates = {
        "is_active": False,
    }
    try:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason or "系统记录：常用箱移入垃圾站",
            source="api.products.delete",
            action="soft_delete",
            confirmation_token=payload.confirmation_token,
            force_version=True,
        )
        product.deleted_at = beijing_now_naive()
        product.deleted_by = user.id
        product.purged_at = None
        audit_master_change(
            db,
            user=user,
            action="MOVE_TO_TRASH",
            resource="Product",
            resource_id=product.id,
            details={
                "customer_id": product.customer_id,
                "product_code": product.product_code,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    return _response(product, user)
