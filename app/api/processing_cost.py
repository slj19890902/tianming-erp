from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.cost_accounting import (
    _audit,
    _idempotency_replay,
    _record_idempotency,
    _request_hash,
    require_cost_manage,
    require_cost_read,
)
from app.api.deps import get_db
from app.core.time_contract import beijing_now_naive
from app.models.customer import Customer
from app.models.processing_cost import ProcessingCostSettings, ProductProcessingProfile
from app.models.product import Product
from app.models.user import User
from app.services.processing_cost import (
    calculate_worker_day_cost,
    get_processing_cost_settings,
)


router = APIRouter()

PRINTER_MODES = (
    {"value": "auto", "label": "自动"},
    {"value": "new", "label": "新印刷机"},
    {"value": "old", "label": "旧印刷机"},
    {"value": "none", "label": "不印刷"},
)
DIE_CUT_MODES = (
    {"value": "auto", "label": "自动"},
    {"value": "none", "label": "不模切"},
    {"value": "small_normal", "label": "小模切-普通"},
    {"value": "small_complex", "label": "小模切-复杂"},
    {"value": "large", "label": "大模切"},
    {"value": "oversize", "label": "超大/往返模切"},
)
DEFAULT_PRINTER_MODES = (
    {"value": "new", "label": "新印刷机"},
    {"value": "old", "label": "旧印刷机"},
)
PRINTER_MODE_VALUES = frozenset(item["value"] for item in PRINTER_MODES)
DIE_CUT_MODE_VALUES = frozenset(item["value"] for item in DIE_CUT_MODES)


class ProcessingSettingsFields(BaseModel):
    working_hours_per_day: Decimal
    working_days_per_month: Decimal
    default_printer: str
    new_printer_normal_sheets_per_minute: Decimal
    new_printer_max_sheets_per_minute: Decimal
    old_printer_normal_sheets_per_minute: Decimal
    old_printer_max_sheets_per_minute: Decimal
    printing_setup_minutes: Decimal
    printing_crew_size: int = Field(gt=0, le=100)
    colors_per_pass: int = Field(gt=0, le=10)
    die_setup_minutes: Decimal
    small_die_normal_pieces_per_minute: Decimal
    small_die_max_pieces_per_minute: Decimal
    small_die_crew_size: int = Field(gt=0, le=100)
    small_complex_die_crew_size: int = Field(gt=0, le=100)
    large_die_pieces_per_minute: Decimal
    large_die_crew_size: int = Field(gt=0, le=100)
    oversize_die_seconds_per_piece: Decimal
    oversize_die_crew_size: int = Field(gt=0, le=100)
    joining_normal_pieces_per_second: Decimal
    joining_max_pieces_per_second: Decimal
    double_splice_seconds_per_piece: Decimal
    joining_crew_size: int = Field(gt=0, le=100)
    average_worker_monthly_salary: Decimal | None = None
    average_worker_monthly_social_cost: Decimal = Decimal("0")

    @field_validator(
        "working_hours_per_day",
        "working_days_per_month",
        "new_printer_normal_sheets_per_minute",
        "new_printer_max_sheets_per_minute",
        "old_printer_normal_sheets_per_minute",
        "old_printer_max_sheets_per_minute",
        "printing_setup_minutes",
        "die_setup_minutes",
        "small_die_normal_pieces_per_minute",
        "small_die_max_pieces_per_minute",
        "large_die_pieces_per_minute",
        "oversize_die_seconds_per_piece",
        "joining_normal_pieces_per_second",
        "joining_max_pieces_per_second",
        "double_splice_seconds_per_piece",
        "average_worker_monthly_salary",
        "average_worker_monthly_social_cost",
    )
    @classmethod
    def finite_decimals(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("参数必须是有效数字")
        return value

    @field_validator("default_printer")
    @classmethod
    def valid_default_printer(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"new", "old"}:
            raise ValueError("默认印刷机只能选择 new 或 old")
        return normalized

    @model_validator(mode="after")
    def validate_ranges(self):
        positive = (
            "working_hours_per_day",
            "working_days_per_month",
            "new_printer_normal_sheets_per_minute",
            "new_printer_max_sheets_per_minute",
            "old_printer_normal_sheets_per_minute",
            "old_printer_max_sheets_per_minute",
            "small_die_normal_pieces_per_minute",
            "small_die_max_pieces_per_minute",
            "large_die_pieces_per_minute",
            "oversize_die_seconds_per_piece",
            "joining_normal_pieces_per_second",
            "joining_max_pieces_per_second",
            "double_splice_seconds_per_piece",
        )
        if any(Decimal(getattr(self, name)) <= 0 for name in positive):
            raise ValueError("速度、工时和工作日参数必须大于 0")
        if self.working_hours_per_day > 24 or self.working_days_per_month > 31:
            raise ValueError("工作时长或工作日超出有效范围")
        if self.printing_setup_minutes < 0 or self.die_setup_minutes < 0:
            raise ValueError("换款时间不能小于 0")
        if self.printing_setup_minutes > 1440 or self.die_setup_minutes > 1440:
            raise ValueError("换款时间不能超过 1440 分钟")
        if (
            self.new_printer_max_sheets_per_minute
            < self.new_printer_normal_sheets_per_minute
            or self.old_printer_max_sheets_per_minute
            < self.old_printer_normal_sheets_per_minute
            or self.small_die_max_pieces_per_minute
            < self.small_die_normal_pieces_per_minute
            or self.joining_max_pieces_per_second
            < self.joining_normal_pieces_per_second
        ):
            raise ValueError("最高速度不能低于默认核算速度")
        if (
            self.average_worker_monthly_salary is not None
            and self.average_worker_monthly_salary <= 0
        ):
            raise ValueError("平均生产月薪为空或大于 0")
        if self.average_worker_monthly_social_cost < 0:
            raise ValueError("平均生产月社保不能小于 0")
        return self


class ProcessingSettingsUpdate(ProcessingSettingsFields):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键长度不能少于 8 个字符")
        return normalized


class ProductProcessingProfileFields(BaseModel):
    printer_mode: str = "auto"
    die_cut_mode: str = "auto"
    assembly_worker_days_per_1000: Decimal | None = None
    assembly_workers: Decimal | None = None
    assembly_days: Decimal | None = None
    assembly_output_quantity: Decimal | None = None

    @field_validator("printer_mode")
    @classmethod
    def valid_printer_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in PRINTER_MODE_VALUES:
            raise ValueError("印刷机模式无效")
        return normalized

    @field_validator("die_cut_mode")
    @classmethod
    def valid_die_cut_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in DIE_CUT_MODE_VALUES:
            raise ValueError("模切档位无效")
        return normalized

    @field_validator("assembly_worker_days_per_1000")
    @classmethod
    def valid_assembly_days(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("每千套组装工日必须为空或大于 0")
        return value

    @field_validator(
        "assembly_workers", "assembly_days", "assembly_output_quantity"
    )
    @classmethod
    def valid_assembly_source(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("组装人数、天数和完成数量必须大于 0")
        return value

    @model_validator(mode="after")
    def calculate_assembly_days_per_1000(self):
        source_values = (
            self.assembly_workers,
            self.assembly_days,
            self.assembly_output_quantity,
        )
        if any(value is not None for value in source_values):
            if not all(value is not None for value in source_values):
                raise ValueError("组装人数、天数和完成数量必须同时填写")
            self.assembly_worker_days_per_1000 = (
                Decimal(self.assembly_workers)
                * Decimal(self.assembly_days)
                * Decimal("1000")
                / Decimal(self.assembly_output_quantity)
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        return self


class ProductProcessingProfileCreate(ProductProcessingProfileFields):
    product_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键长度不能少于 8 个字符")
        return normalized


class ProductProcessingProfileUpdate(ProductProcessingProfileFields):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键长度不能少于 8 个字符")
        return normalized


class ProductProcessingProfileDelete(BaseModel):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("幂等键长度不能少于 8 个字符")
        return normalized


def _enum_metadata() -> dict[str, list[dict[str, str]]]:
    return {
        "printer_modes": [dict(item) for item in PRINTER_MODES],
        "die_cut_modes": [dict(item) for item in DIE_CUT_MODES],
        "default_printer_modes": [dict(item) for item in DEFAULT_PRINTER_MODES],
    }


SETTINGS_FIELDS = tuple(ProcessingSettingsFields.model_fields)


def _settings_response(row: ProcessingCostSettings) -> dict[str, Any]:
    worker_day_cost = calculate_worker_day_cost(row)
    return {
        "id": row.id,
        **{name: getattr(row, name) for name in SETTINGS_FIELDS},
        "worker_day_cost": worker_day_cost,
        "worker_day_cost_status": (
            "calculated" if worker_day_cost is not None else "incomplete"
        ),
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        **_enum_metadata(),
    }


def _profile_response(
    product: Product,
    customer: Customer,
    row: ProductProcessingProfile | None,
) -> dict[str, Any]:
    is_override = bool(
        row
        and (
            row.printer_mode != "auto"
            or row.die_cut_mode != "auto"
            or row.assembly_worker_days_per_1000 is not None
        )
    )
    return {
        "id": row.id if row else None,
        "product_id": product.id,
        "customer_id": customer.id,
        "customer_name": customer.name,
        "customer_material_code": product.customer_material_code,
        "product_code": product.product_code,
        "product_name": product.product_name,
        "printer_mode": row.printer_mode if row else "auto",
        "die_cut_mode": row.die_cut_mode if row else "auto",
        "assembly_worker_days_per_1000": (
            row.assembly_worker_days_per_1000 if row else None
        ),
        "version": row.version if row else 0,
        "is_override": is_override,
        "updated_at": row.updated_at if row else None,
    }


def _product_context(
    db: Session, product_id: int
) -> tuple[Product, Customer, ProductProcessingProfile | None]:
    result = db.execute(
        select(Product, Customer, ProductProcessingProfile)
        .join(Customer, Customer.id == Product.customer_id)
        .outerjoin(
            ProductProcessingProfile,
            ProductProcessingProfile.product_id == Product.id,
        )
        .where(Product.id == product_id, Product.deleted_at.is_(None))
    ).one_or_none()
    if result is None:
        raise HTTPException(status_code=404, detail="产品不存在")
    return result


def _profile_values(payload: ProductProcessingProfileFields) -> dict[str, Any]:
    return payload.model_dump(
        exclude={
            "product_id",
            "expected_version",
            "idempotency_key",
            "assembly_workers",
            "assembly_days",
            "assembly_output_quantity",
        }
    )


@router.get("/processing-settings")
def get_processing_settings(
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_read),
) -> dict[str, Any]:
    try:
        return _settings_response(get_processing_cost_settings(db))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.put("/processing-settings")
def update_processing_settings(
    payload: ProcessingSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_manage),
) -> dict[str, Any]:
    action = "finance.processing_settings.update"
    request_hash = _request_hash(action, payload.model_dump())
    replay = _idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action=action,
        actor=user,
    )
    if replay is not None:
        return replay
    try:
        row = db.get(ProcessingCostSettings, 1)
        if row is None:
            raise HTTPException(status_code=409, detail="加工成本参数尚未初始化")
        if row.version != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"message": "加工成本参数版本已变化", "current_version": row.version},
            )
        before = _settings_response(row)
        values = payload.model_dump(exclude={"expected_version", "idempotency_key"})
        result = db.execute(
            update(ProcessingCostSettings)
            .where(
                ProcessingCostSettings.id == 1,
                ProcessingCostSettings.version == payload.expected_version,
            )
            .values(
                **values,
                version=payload.expected_version + 1,
                updated_by=user.id,
                updated_at=beijing_now_naive(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.expire_all()
            current = db.get(ProcessingCostSettings, 1)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "加工成本参数版本已变化",
                    "current_version": current.version if current else None,
                },
            )
        db.expire(row)
        db.refresh(row)
        response = _settings_response(row)
        _audit(
            db,
            request=request,
            user=user,
            action=action,
            resource="ProcessingCostSettings",
            entity_id=row.id,
            description="更新标准工时与加工成本参数",
            details={"before": before, "after": response},
        )
        _record_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
            resource_type="processing_cost_settings",
            resource_id=row.id,
            response=response,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError as error:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise HTTPException(status_code=409, detail="加工成本参数或幂等键冲突") from error
    except Exception:
        db.rollback()
        raise


@router.get("/product-processing-profiles")
def list_product_processing_profiles(
    search: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_read),
) -> dict[str, Any]:
    query = (
        select(Product, Customer, ProductProcessingProfile)
        .join(Customer, Customer.id == Product.customer_id)
        .outerjoin(
            ProductProcessingProfile,
            ProductProcessingProfile.product_id == Product.id,
        )
        .where(Product.deleted_at.is_(None), Product.is_active.is_(True))
    )
    normalized = str(search or "").strip()
    if normalized:
        term = f"%{normalized}%"
        query = query.where(
            or_(
                Product.customer_material_code.ilike(term),
                Product.product_code.ilike(term),
                Product.product_name.ilike(term),
                Customer.name.ilike(term),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = db.execute(
        query.order_by(Customer.name, Product.customer_material_code, Product.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_profile_response(*row) for row in rows],
        **_enum_metadata(),
    }


@router.get("/product-processing-profiles/{product_id}")
def get_product_processing_profile_api(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_read),
) -> dict[str, Any]:
    product, customer, row = _product_context(db, product_id)
    return {**_profile_response(product, customer, row), **_enum_metadata()}


def _create_profile(
    db: Session,
    *,
    product: Product,
    payload: ProductProcessingProfileFields,
    user: User,
) -> ProductProcessingProfile:
    row = ProductProcessingProfile(
        product_id=product.id,
        **_profile_values(payload),
        version=1,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(row)
    db.flush()
    return row


@router.post("/product-processing-profiles", status_code=status.HTTP_201_CREATED)
def create_product_processing_profile(
    payload: ProductProcessingProfileCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_manage),
) -> dict[str, Any]:
    action = "finance.product_processing_profile.create"
    request_hash = _request_hash(action, payload.model_dump())
    replay = _idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action=action,
        actor=user,
    )
    if replay is not None:
        return replay
    try:
        product, customer, existing = _product_context(db, payload.product_id)
        if existing is not None:
            raise HTTPException(status_code=409, detail="该产品已有加工例外")
        row = _create_profile(db, product=product, payload=payload, user=user)
        response = _profile_response(product, customer, row)
        _audit(
            db,
            request=request,
            user=user,
            action=action,
            resource="ProductProcessingProfile",
            entity_id=row.id,
            description="新增产品加工例外",
            details=response,
        )
        _record_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
            resource_type="product_processing_profile",
            resource_id=row.id,
            response=response,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError as error:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise HTTPException(status_code=409, detail="产品加工例外或幂等键冲突") from error
    except Exception:
        db.rollback()
        raise


@router.put("/product-processing-profiles/{product_id}")
def upsert_product_processing_profile(
    product_id: int,
    payload: ProductProcessingProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_manage),
) -> dict[str, Any]:
    action = "finance.product_processing_profile.upsert"
    request_hash = _request_hash(
        action, {"product_id": product_id, **payload.model_dump()}
    )
    replay = _idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action=action,
        actor=user,
    )
    if replay is not None:
        return replay
    try:
        product, customer, row = _product_context(db, product_id)
        if row is None:
            if payload.expected_version != 0:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "产品加工例外不存在", "current_version": 0},
                )
            before = _profile_response(product, customer, None)
            row = _create_profile(db, product=product, payload=payload, user=user)
        else:
            if payload.expected_version != row.version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "产品加工例外版本已变化",
                        "current_version": row.version,
                    },
                )
            before = _profile_response(product, customer, row)
            result = db.execute(
                update(ProductProcessingProfile)
                .where(
                    ProductProcessingProfile.id == row.id,
                    ProductProcessingProfile.version == payload.expected_version,
                )
                .values(
                    **_profile_values(payload),
                    version=payload.expected_version + 1,
                    updated_by=user.id,
                    updated_at=beijing_now_naive(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                db.expire_all()
                current = db.get(ProductProcessingProfile, row.id)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "产品加工例外版本已变化",
                        "current_version": current.version if current else 0,
                    },
                )
            db.expire(row)
            db.refresh(row)
        response = _profile_response(product, customer, row)
        _audit(
            db,
            request=request,
            user=user,
            action=action,
            resource="ProductProcessingProfile",
            entity_id=row.id,
            description="保存产品加工例外",
            details={"before": before, "after": response},
        )
        _record_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
            resource_type="product_processing_profile",
            resource_id=row.id,
            response=response,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError as error:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise HTTPException(status_code=409, detail="产品加工例外或幂等键冲突") from error
    except Exception:
        db.rollback()
        raise


@router.delete("/product-processing-profiles/{product_id}")
def delete_product_processing_profile(
    product_id: int,
    payload: ProductProcessingProfileDelete,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_cost_manage),
) -> dict[str, Any]:
    action = "finance.product_processing_profile.delete"
    request_hash = _request_hash(
        action, {"product_id": product_id, **payload.model_dump()}
    )
    replay = _idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action=action,
        actor=user,
    )
    if replay is not None:
        return replay
    try:
        product, customer, row = _product_context(db, product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="产品加工例外不存在")
        if row.version != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "产品加工例外版本已变化",
                    "current_version": row.version,
                },
            )
        before = _profile_response(product, customer, row)
        resource_id = row.id
        result = db.execute(
            update(ProductProcessingProfile)
            .where(
                ProductProcessingProfile.id == resource_id,
                ProductProcessingProfile.version == payload.expected_version,
            )
            .values(
                printer_mode="auto",
                die_cut_mode="auto",
                assembly_worker_days_per_1000=None,
                version=payload.expected_version + 1,
                updated_by=user.id,
                updated_at=beijing_now_naive(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.expire_all()
            current = db.get(ProductProcessingProfile, resource_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "产品加工例外版本已变化",
                    "current_version": current.version if current else None,
                },
            )
        db.expire(row)
        db.refresh(row)
        response = {
            **_profile_response(product, customer, row),
            "restored_default": True,
        }
        _audit(
            db,
            request=request,
            user=user,
            action=action,
            resource="ProductProcessingProfile",
            entity_id=resource_id,
            description="恢复产品自动加工规则",
            details={"before": before, "after": response},
        )
        _record_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
            resource_type="product_processing_profile",
            resource_id=resource_id,
            response=response,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise
    except IntegrityError as error:
        db.rollback()
        replay = _idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action=action,
            actor=user,
        )
        if replay is not None:
            return replay
        raise HTTPException(status_code=409, detail="删除产品加工例外失败") from error
    except Exception:
        db.rollback()
        raise
