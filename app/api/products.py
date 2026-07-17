from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, cast, delete, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import (
    beijing_naive_to_api,
    beijing_now_naive,
    utc_naive_to_api,
)
from app.api.master_data_common import audit_master_change, clean_code
from app.models.customer import Customer
from app.models.material import Material
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
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
from app.services.product_lifecycle import (
    has_historical_references,
)
from app.services.report_crease import crease_width_error
from app.services.master_data_versioning import (
    apply_versioned_update,
    record_versioned_create,
    serialize_versioned_entity,
)


router = APIRouter()
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
    value = (box_style or "").strip().upper()
    return bool(value) and ("A1" in value or "0201" in value)


def _box_style_uses_tongue(box_style: str | None) -> bool:
    value = (box_style or "").strip()
    if not value:
        return True
    upper = value.upper()
    return (
        "A1" in upper
        or "0201" in upper
        or "围套" in value
        or "半开槽" in value
        or "全搭盖" in value
    )


def _production_process_uses_mold(value: str | None) -> bool:
    return "模切" in {
        item.strip()
        for item in re.split(r"[,，、]", str(value or ""))
        if item.strip()
    }


def _normalize_product_mold_binding(payload: ProductPayload) -> None:
    if _production_process_uses_mold(payload.production_process):
        if payload.mold_tool_id is None:
            raise HTTPException(
                status_code=400,
                detail="生产工艺包含模切，必须选择已登记的生产模具及货架位置",
            )
        return
    payload.mold_tool_id = None


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
    print_content: str | None = None
    printing_colors: str | None = None
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
    flap_mm: int | None = 30

    @model_validator(mode="after")
    def validate_flute_layer_consistency(self) -> "ProductPayload":
        """拒绝非法楞型/层数组合；七层写入必须明确 AAA/ABC。"""
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
        self.box_style = (self.box_style or "").strip() or None
        splice_mode = (self.splice_mode or "single").strip().lower()
        if _box_style_uses_splice(self.box_style):
            if splice_mode not in {"single", "double"}:
                raise ValueError("拼箱方式仅允许：single 或 double")
            self.splice_mode = splice_mode
            if self.pieces_per_box is None:
                self.pieces_per_box = 2 if splice_mode == "double" else 1
            if self.pieces_per_box not in {1, 2}:
                raise ValueError("每箱片数仅允许 1 或 2")
        elif self.box_style:
            self.splice_mode = "single"
            self.pieces_per_box = 1
        else:
            if splice_mode not in {"single", "double"}:
                raise ValueError("拼箱方式仅允许：single 或 double")
            self.splice_mode = splice_mode
            if self.pieces_per_box is None:
                self.pieces_per_box = 2 if splice_mode == "double" else 1
            if self.pieces_per_box not in {1, 2}:
                raise ValueError("每箱片数仅允许 1 或 2")
        if _box_style_uses_tongue(self.box_style):
            if self.flap_mm is None:
                self.flap_mm = 30
            if self.flap_mm <= 0:
                raise ValueError("舌头(mm)必须大于0")
        else:
            self.flap_mm = None
        return self


class ProductDrawingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    image_path: str
    thumbnail_path: str
    uploaded_at: datetime


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
    drawings: list[ProductDrawingResponse] = Field(default_factory=list)


class ProductMutationPayload(BaseModel):
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1)
    confirmation_token: str | None = None

    @field_validator("change_reason")
    @classmethod
    def validate_change_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("修改原因不能为空")
        return reason


class ProductUpdatePayload(ProductPayload):
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=1)
    confirmation_token: str | None = None

    @field_validator("change_reason")
    @classmethod
    def validate_change_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("修改原因不能为空")
        return reason


class ProductStatusPayload(ProductMutationPayload):
    is_active: bool


class ProductTrashEmptyPayload(BaseModel):
    expected_versions: dict[int, int]
    change_reason: str = Field(min_length=1)
    confirmation_tokens: dict[int, str] = Field(default_factory=dict)

    @field_validator("expected_versions")
    @classmethod
    def validate_expected_versions(cls, value: dict[int, int]) -> dict[int, int]:
        if any(version < 1 for version in value.values()):
            raise ValueError("预期版本必须大于等于1")
        return value

    @field_validator("change_reason")
    @classmethod
    def validate_change_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("修改原因不能为空")
        return reason


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


def _product_payload_snapshot(product: Product) -> dict:
    """Serialize stored product values without applying write-time validators.

    Historical products may contain incomplete or legacy layer/flute values.
    Reads must remain available so an operator can inspect and correct them;
    create/update requests still use ProductPayload and its strict validators.
    """
    return {
        field_name: getattr(product, field_name, None)
        for field_name in ProductPayload.model_fields
    }


def _product_write_data(payload: ProductPayload, user: User) -> dict:
    """Return the subset of product fields this user may persist.

    Sales staff may continue to create and edit ordinary product fields, but
    cost-sensitive values supplied by a client are never persisted without the
    cost.view permission.  On updates their stored values are consequently
    retained; on creates they keep the model defaults.
    """
    data = payload.model_dump(include=set(ProductPayload.model_fields))
    if not has_permission(user, "cost.view"):
        for field in _COST_SENSITIVE_PRODUCT_FIELDS:
            data.pop(field, None)
    return data


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
        "drawings": [
            {
                **ProductDrawingResponse.model_validate(drawing).model_dump(),
                "uploaded_at": utc_naive_to_api(drawing.uploaded_at),
            }
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


def _changed_updates(product: Product, updates: dict) -> dict:
    return {
        key: value
        for key, value in updates.items()
        if getattr(product, key, None) != value
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
) -> None:
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    if material_id is not None and db.get(Material, material_id) is None:
        raise HTTPException(status_code=400, detail="材质不存在")
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


@router.get("")
def list_products(
    customer_id: int | None = None,
    keyword: str = "",
    product_code: str = "",
    product_name: str = "",
    spec: str = "",
    material: str = "",
    include_inactive: bool = False,
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
    items = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total else 0,
        "items": [_response(item, user) for item in items],
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
                details={**details, "reason": payload.change_reason},
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


async def _create_drawing_version(
    product_id: int,
    file: UploadFile,
    db: Session,
    user: User,
) -> ProductDrawing:
    product = _product_or_404(db, product_id)
    require_customer_access(product.customer_id, current_user=user, db=db)
    content = await file.read()
    try:
        saved = save_product_drawing_files(
            product_id=product.id,
            content=content,
            content_type=file.content_type,
        )
    except DrawingValidationError as error:
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
            "image_path": saved.image_path,
            "thumbnail_path": saved.thumbnail_path,
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
    return ProductDrawingResponse.model_validate(drawing).model_dump()


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
        "drawing_path": drawing.image_path,
        "image_path": drawing.image_path,
        "thumbnail_path": drawing.thumbnail_path,
    }


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
    _normalize_product_mold_binding(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
    )
    _validate_product_material_flute(db, payload)
    _validate_product_crease_widths(payload)
    data = _product_write_data(payload, user)
    data.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
        manual_modified=True,
        manual_modified_at=beijing_now_naive(),
    )
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
        raise HTTPException(status_code=409, detail="同客户产品编码或客户料号重复") from error
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
    _normalize_product_mold_binding(payload)
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
        mold_tool_id=payload.mold_tool_id,
    )
    _validate_product_material_flute(db, payload)
    _validate_changed_product_crease_widths(payload, product)
    before = _product_payload_snapshot(product)
    updates = _product_write_data(payload, user)
    versioned_fields = set(serialize_versioned_entity("product", product))
    updates = {
        key: value for key, value in updates.items() if key in versioned_fields
    }
    updates.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
    )
    changed = _changed_updates(product, updates)
    try:
        apply_versioned_update(
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
        product.manual_modified = True
        product.manual_modified_at = beijing_now_naive()
        if changed:
            audit_master_change(
                db,
                user=user,
                action="UPDATE",
                resource="Product",
                resource_id=product.id,
                details={"before": before, "after": updates},
            )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="同客户产品编码或客户料号重复") from error
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
            reason=payload.change_reason,
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
            reason=payload.change_reason,
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
                "reason": payload.change_reason,
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
    change_reason: str = Field(min_length=1)
    confirmation_token: str | None = None

    @field_validator("change_reason")
    @classmethod
    def validate_change_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("修改原因不能为空")
        return reason


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
    if not has_permission(user, "cost.view"):
        for field in _COST_SENSITIVE_PRODUCT_FIELDS:
            payload.fields.pop(field, None)
    requested_layer = payload.fields.get("layer_count", product.layer_count)
    requested_flute = normalize_flute_type(
        payload.fields.get("flute_type", product.flute_type)
    )
    if "layer_count" in payload.fields:
        requested_error = validate_flute_for_write(requested_flute, requested_layer)
        if requested_error:
            raise HTTPException(status_code=400, detail=requested_error)
    selected_material_id = payload.fields.get("material_id", product.material_id)
    selected_material = (
        db.get(Material, selected_material_id)
        if selected_material_id is not None
        else None
    )
    if selected_material_id is not None and selected_material is None:
        raise HTTPException(status_code=400, detail="材质不存在")
    if (
        selected_material is not None
        and "layer_count" in payload.fields
        and payload.fields["layer_count"] is not None
        and payload.fields["layer_count"] != selected_material.layer_count
    ):
        raise HTTPException(status_code=400, detail="请求层数与所选材质真实层数不一致")
    prospective_layer = (
        selected_material.layer_count
        if selected_material is not None
        else payload.fields.get("layer_count", product.layer_count)
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
        payload.fields.get("flute_type", product.flute_type)
    )
    flute_fields = {"material_id", "layer_count", "flute_type"}
    flute_error = validate_flute_for_write(prospective_flute, prospective_layer)
    if flute_error:
        raise HTTPException(status_code=400, detail=flute_error)
    if selected_material is not None and flute_fields.intersection(payload.fields):
        payload.fields["layer_count"] = prospective_layer
    if "production_process" in payload.fields or "mold_tool_id" in payload.fields:
        prospective_process = payload.fields.get(
            "production_process", product.production_process
        )
        prospective_mold_id = payload.fields.get(
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
            payload.fields["mold_tool_id"] = None
    main_crease_fields = {
        "report_width_mm",
        "crease_type",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
    }
    if main_crease_fields.intersection(payload.fields):
        error = _crease_width_error(
            label="压线",
            crease_type=payload.fields.get("crease_type", product.crease_type),
            report_width_mm=payload.fields.get(
                "report_width_mm", product.report_width_mm
            ),
            left_mm=payload.fields.get("crease_left_mm", product.crease_left_mm),
            middle_mm=payload.fields.get(
                "crease_middle_mm", product.crease_middle_mm
            ),
            right_mm=payload.fields.get(
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
    if base_crease_fields.intersection(payload.fields):
        error = _crease_width_error(
            label="底压线",
            crease_type=payload.fields.get(
                "base_crease_type", product.base_crease_type
            ),
            report_width_mm=payload.fields.get(
                "base_report_width_mm", product.base_report_width_mm
            ),
            left_mm=payload.fields.get(
                "base_crease_left_mm", product.base_crease_left_mm
            ),
            middle_mm=payload.fields.get(
                "base_crease_middle_mm", product.base_crease_middle_mm
            ),
            right_mm=payload.fields.get(
                "base_crease_right_mm", product.base_crease_right_mm
            ),
        )
        if error:
            raise HTTPException(status_code=400, detail=error)
    updates = {}
    versioned_fields = set(serialize_versioned_entity("product", product))
    for k, v in payload.fields.items():
        if k not in ALLOWED:
            continue
        if k not in Product.__table__.columns or k not in versioned_fields:
            continue
        if k == "flute_type":
            v = prospective_flute
        updates[k] = v
    if not updates:
        raise HTTPException(status_code=400, detail="没有可同步的字段")
    changed = _changed_updates(product, updates)
    try:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
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
            reason=payload.change_reason,
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
