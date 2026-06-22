from __future__ import annotations

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
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.api.master_data_common import audit_master_change, clean_code
from app.models.customer import Customer
from app.models.material import Material
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services.product_drawings import (
    DrawingValidationError,
    remove_drawing_files,
    save_product_drawing_files,
)
from app.services.product_lifecycle import (
    archive_purged_product,
    has_historical_references,
    move_to_trash,
    restore_from_trash,
)


router = APIRouter()
can_read = RoleChecker(["admin", "sales", "workshop"])
can_write = RoleChecker(["admin"])
admin_only = RoleChecker(["admin"])
PRODUCT_DELETE_CONFLICT_DETAIL = (
    "该纸箱已在历史订单、报料或库存中使用，为了保证历史账目完整，"
    "系统禁止直接删除。请使用【停用】功能。"
)


class ProductPayload(BaseModel):
    customer_id: int
    product_code: str = Field(min_length=1, max_length=150)
    customer_material_code: str = Field(min_length=1, max_length=150)
    product_name: str = Field(min_length=1, max_length=250)
    material_id: int | None = None
    legacy_material_text: str | None = None
    length_mm: Decimal | None = Field(default=None, gt=0)
    width_mm: Decimal | None = Field(default=None, gt=0)
    height_mm: Decimal | None = Field(default=None, gt=0)
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
    drawings: list[ProductDrawingResponse] = Field(default_factory=list)


class ProductStatusPayload(BaseModel):
    is_active: bool


PRICE_FIELDS = {
    "sale_unit_price",
    "sale_unit_price_no_tax",
    "cost_unit_price",
    "board_price",
    "suggested_price",
}
def _product_or_404(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="产品不存在")
    return product


def _response(product: Product, user: User) -> dict:
    data = ProductResponse.model_validate(product).model_dump()
    if product.deleted_at is not None:
        expires_at = product.deleted_at + timedelta(days=30)
        data["deleted_expires_at"] = expires_at
        data["trash_days_remaining"] = max(
            0,
            (expires_at - datetime.now()).total_seconds() / 86400,
        )
    if user.role == "workshop":
        for field in PRICE_FIELDS:
            data.pop(field, None)
    return data


def _validate_references(
    db: Session,
    *,
    customer_id: int,
    material_id: int | None,
) -> None:
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    if material_id is not None and db.get(Material, material_id) is None:
        raise HTTPException(status_code=400, detail="材质不存在")


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
    if customer_id is not None:
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
    expired = db.scalars(
        select(Product).where(
            Product.deleted_at.is_not(None),
            Product.purged_at.is_(None),
            Product.deleted_at <= datetime.now() - timedelta(days=30),
        )
    ).all()
    for product in expired:
        details = {
            "customer_id": product.customer_id,
            "product_code": product.product_code,
            "reason": "30_day_expired",
        }
        if has_historical_references(db, product.id):
            archive_purged_product(product, user)
            mode = "archive"
        else:
            db.delete(product)
            mode = "physical_delete"
        audit_master_change(
            db,
            user=user,
            action="PURGE",
            resource="Product",
            resource_id=product.id,
            details={**details, "mode": mode},
        )
    if expired:
        db.commit()
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
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    products = db.scalars(
        select(Product).where(
            Product.deleted_at.is_not(None),
            Product.purged_at.is_(None),
        )
    ).all()
    deleted_count = 0
    archived_count = 0
    for product in products:
        details = {
            "customer_id": product.customer_id,
            "product_code": product.product_code,
        }
        if has_historical_references(db, product.id):
            archive_purged_product(product, user)
            archived_count += 1
            action = "PURGE_ARCHIVE"
        else:
            db.delete(product)
            deleted_count += 1
            action = "PURGE"
        audit_master_change(
            db,
            user=user,
            action=action,
            resource="Product",
            resource_id=product.id,
            details=details,
        )
    db.commit()
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
    return _response(_product_or_404(db, product_id), user)


async def _create_drawing_version(
    product_id: int,
    file: UploadFile,
    db: Session,
    user: User,
) -> ProductDrawing:
    product = _product_or_404(db, product_id)
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
    user: User = Depends(can_write),
) -> dict:
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
    )
    data = payload.model_dump()
    data.update(
        product_code=clean_code(payload.product_code),
        customer_material_code=clean_code(payload.customer_material_code),
        product_name=payload.product_name.strip(),
    )
    product = Product(**data)
    try:
        db.add(product)
        db.flush()
        audit_master_change(
            db,
            user=user,
            action="CREATE",
            resource="Product",
            resource_id=product.id,
            details=payload.model_dump(),
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="同客户产品编码或客户料号重复") from error
    db.refresh(product)
    return _response(product, user)


@router.put("/{product_id}")
def update_product(
    product_id: int,
    payload: ProductPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    _validate_references(
        db,
        customer_id=payload.customer_id,
        material_id=payload.material_id,
    )
    product = _product_or_404(db, product_id)
    before = ProductResponse.model_validate(product).model_dump()
    for key, value in payload.model_dump().items():
        setattr(product, key, value)
    product.product_code = clean_code(payload.product_code)
    product.customer_material_code = clean_code(payload.customer_material_code)
    product.product_name = payload.product_name.strip()
    try:
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="Product",
            resource_id=product.id,
            details={"before": before, "after": payload.model_dump()},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="同客户产品编码或客户料号重复") from error
    db.refresh(product)
    return _response(product, user)


@router.put("/{product_id}/status")
def update_product_status(
    product_id: int,
    payload: ProductStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    product = _product_or_404(db, product_id)
    if product.deleted_at is not None:
        raise HTTPException(status_code=400, detail="请先从垃圾站恢复该纸箱")
    before = product.is_active
    product.is_active = payload.is_active
    audit_master_change(
        db,
        user=user,
        action="ENABLE" if payload.is_active else "DISABLE",
        resource="Product",
        resource_id=product.id,
        details={"before": before, "after": payload.is_active},
    )
    db.commit()
    db.refresh(product)
    return _response(product, user)


@router.put("/{product_id}/restore")
def restore_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    product = _product_or_404(db, product_id)
    if product.deleted_at is None:
        raise HTTPException(status_code=400, detail="该纸箱不在垃圾站中")
    if product.purged_at is not None:
        raise HTTPException(status_code=409, detail="该纸箱已永久归档，不能恢复")
    restore_from_trash(product)
    audit_master_change(
        db,
        user=user,
        action="RESTORE",
        resource="Product",
        resource_id=product.id,
        details={"product_code": product.product_code},
    )
    db.commit()
    db.refresh(product)
    return _response(product, user)


@router.delete("/{product_id}/purge", response_model=None)
def purge_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> Response | dict:
    product = _product_or_404(db, product_id)
    if product.deleted_at is None:
        raise HTTPException(status_code=400, detail="请先将该纸箱移入垃圾站")
    details = {
        "customer_id": product.customer_id,
        "product_code": product.product_code,
    }
    if has_historical_references(db, product.id):
        archive_purged_product(product, user)
        audit_master_change(
            db,
            user=user,
            action="PURGE",
            resource="Product",
            resource_id=product.id,
            details={**details, "mode": "archive"},
        )
        db.commit()
        db.refresh(product)
        return _response(product, user)
    audit_master_change(
        db,
        user=user,
        action="PURGE",
        resource="Product",
        resource_id=product.id,
        details={**details, "mode": "physical_delete"},
    )
    db.delete(product)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    product = _product_or_404(db, product_id)
    if product.deleted_at is not None:
        raise HTTPException(status_code=400, detail="该纸箱已在垃圾站中")
    move_to_trash(product, user)
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
    db.refresh(product)
    return _response(product, user)
