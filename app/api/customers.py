from __future__ import annotations

from decimal import Decimal
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.api.master_data_common import audit_master_change, clean_code
from app.models.customer import Customer
from app.models.customer_quote_preference import CustomerQuotePreference
from app.models.material import Material
from app.models.access_control import UserCustomerScope
from app.models.order import Order
from app.models.user import User
from app.services.master_data_versioning import (
    apply_versioned_update,
    preview_versioned_update,
    record_versioned_create,
)
from app.services.customer_quote_pricing import (
    CustomerQuotePricingError,
    canonical_quote_box_type,
    estimate_a1_unit_price,
    resolve_customer_square_price,
)
from app.services.flute_mapping import normalize_flute_type, validate_flute_consistency
from app.services.supplier_master import SupplierLookupError, resolve_supplier


router = APIRouter()
can_read = PermissionChecker("customers.view")
can_create = PermissionChecker("customers.create")
can_write = PermissionChecker("customers.edit")
can_deactivate = PermissionChecker("customers.deactivate")
can_delete = PermissionChecker("customers.delete")


class CustomerPayload(BaseModel):
    customer_number: int = Field(gt=0)
    customer_code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    chinese_short_name: str | None = Field(default=None, max_length=30)
    payment_term_days: int = Field(default=0, ge=0)
    statement_cycle_start_day: int = Field(default=20, ge=1, le=28)
    credit_limit: Decimal = Field(default=Decimal("0"), ge=0)
    delivery_method: Literal["自提", "配送", "物流"] = "配送"
    contact_person: str | None = None
    phone: str | None = None
    address: str | None = None
    default_tax_rate: Decimal = Field(default=Decimal("0.13"), ge=0)
    invoice_title: str | None = None
    tax_no: str | None = None
    bank_account: str | None = None
    remark: str | None = None
    status: str = "active"

    @field_validator("chinese_short_name", mode="before")
    @classmethod
    def normalize_chinese_short_name(cls, value: object) -> str | None:
        if value is None:
            return None
        short_name = str(value).strip()
        if not short_name:
            return None
        if not re.search(r"[\u3400-\u9fff]", short_name):
            raise ValueError("标签中文简称必须至少包含一个中文字符")
        return short_name


class CustomerResponse(CustomerPayload):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    version: int


class CustomerMutationPayload(BaseModel):
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


class CustomerUpdatePayload(CustomerPayload):
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


class CustomerUpdatePreviewPayload(CustomerPayload):
    expected_version: int = Field(ge=1)


class CustomerStatusPayload(CustomerMutationPayload):
    is_active: bool


class CustomerQuotePreferenceCreatePayload(BaseModel):
    box_type: str = Field(min_length=1, max_length=150)
    material_id: int = Field(gt=0)
    flute_type: str = Field(min_length=1, max_length=20)
    tax_included_square_price: Decimal = Field(gt=0)
    is_active: bool = True

    @field_validator("box_type", "flute_type")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空")
        return cleaned.upper()


class CustomerQuotePreferenceUpdatePayload(BaseModel):
    tax_included_square_price: Decimal = Field(gt=0)
    is_active: bool
    expected_version: int = Field(ge=1)


class CustomerQuoteEstimatePayload(BaseModel):
    box_type: str = Field(min_length=1, max_length=150)
    material_id: int = Field(gt=0)
    flute_type: str = Field(min_length=1, max_length=20)
    length_mm: Decimal = Field(gt=0)
    width_mm: Decimal = Field(gt=0)
    height_mm: Decimal = Field(gt=0)
    manual_unit_price: Decimal | None = Field(default=None, ge=0)

    @field_validator("box_type", "flute_type")
    @classmethod
    def strip_estimate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空")
        return cleaned.upper()


def _customer_write_data(
    payload: CustomerPayload,
    *,
    existing: Customer | None = None,
) -> dict:
    data = payload.model_dump(include=set(CustomerPayload.model_fields))
    # Older full-update clients do not know this new optional field.  Omission
    # must preserve the operator-maintained value instead of silently clearing
    # it through the Pydantic default.
    if existing is not None and "chinese_short_name" not in payload.model_fields_set:
        data.pop("chinese_short_name", None)
    data.update(
        customer_code=clean_code(payload.customer_code),
        name=payload.name.strip(),
        is_active=payload.status == "active",
    )
    return data


def _changed_updates(customer: Customer, updates: dict) -> dict:
    return {
        key: value
        for key, value in updates.items()
        if getattr(customer, key) != value
    }


def _customer_or_404(db: Session, customer_id: int) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    return customer


def _quote_preference_or_404(
    db: Session, customer_id: int, preference_id: int
) -> CustomerQuotePreference:
    preference = db.scalar(
        select(CustomerQuotePreference).where(
            CustomerQuotePreference.id == preference_id,
            CustomerQuotePreference.customer_id == customer_id,
        )
    )
    if preference is None:
        raise HTTPException(status_code=404, detail="客户报价偏好不存在")
    return preference


def _validated_preference_flute(material: Material, flute_type: str) -> str:
    flute = normalize_flute_type(flute_type)
    error = validate_flute_consistency(flute, material.layer_count)
    if error:
        raise HTTPException(status_code=400, detail=error)
    if not flute:
        raise HTTPException(status_code=400, detail="楞型不能为空")
    return flute


def _require_active_material_supplier(db: Session, material: Material) -> None:
    try:
        resolve_supplier(db, material.supplier_name, require_active=True)
    except SupplierLookupError as error:
        raise HTTPException(status_code=400, detail=error.message) from error


def _normalized_quote_box_type(box_type: str) -> str:
    return canonical_quote_box_type(box_type)


def _require_a1_quote_box_type(box_type: str) -> str:
    normalized = _normalized_quote_box_type(box_type)
    if normalized != "A1":
        raise HTTPException(status_code=400, detail="当前仅支持 A1/0201 尺寸报价试算")
    return normalized


def _quote_preference_response(preference: CustomerQuotePreference) -> dict:
    return {
        "id": preference.id,
        "customer_id": preference.customer_id,
        "box_type": preference.box_type,
        "material_id": preference.material_id,
        "material_code": preference.material.code if preference.material else None,
        "supplier_name": preference.material.supplier_name if preference.material else None,
        "layer_count": preference.material.layer_count if preference.material else None,
        "material_is_active": bool(preference.material and preference.material.is_active),
        "material_display": (
            " / ".join(
                part
                for part in (
                    preference.material.code,
                    preference.material.supplier_name,
                )
                if part
            )
            if preference.material
            else None
        ),
        "flute_type": preference.flute_type,
        "tax_included_square_price": preference.tax_included_square_price,
        "is_active": preference.is_active,
        "version": preference.version,
    }


@router.get("")
def list_customers(
    keyword: str = "",
    include_inactive: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = select(Customer).order_by(Customer.customer_number, Customer.id)
    scoped_ids = customer_scope_ids(user, db)
    if not has_unrestricted_customer_access(user, db):
        query = query.where(Customer.id.in_(scoped_ids))
    if not include_inactive:
        query = query.where(Customer.is_active.is_(True))
    if keyword.strip():
        pattern = f"%{keyword.strip()}%"
        query = query.where(
            or_(
                Customer.name.like(pattern),
                Customer.customer_code.like(pattern),
                Customer.chinese_short_name.like(pattern),
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
        "items": [
            CustomerResponse.model_validate(item).model_dump() for item in items
        ]
    }


@router.get("/{customer_id}")
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> CustomerResponse:
    require_customer_access(customer_id, current_user=user, db=db)
    return CustomerResponse.model_validate(_customer_or_404(db, customer_id))


@router.get("/{customer_id}/quote-preferences")
def list_customer_quote_preferences(
    customer_id: int,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    _customer_or_404(db, customer_id)
    statement = (
        select(CustomerQuotePreference)
        .where(CustomerQuotePreference.customer_id == customer_id)
        .order_by(
            CustomerQuotePreference.box_type,
            CustomerQuotePreference.material_id,
            CustomerQuotePreference.flute_type,
            CustomerQuotePreference.id,
        )
    )
    if not include_inactive:
        statement = statement.where(CustomerQuotePreference.is_active.is_(True))
    preferences = db.scalars(statement).all()
    return {"items": [_quote_preference_response(item) for item in preferences]}


@router.post("/{customer_id}/quote-preferences", status_code=status.HTTP_201_CREATED)
def create_customer_quote_preference(
    customer_id: int,
    payload: CustomerQuotePreferenceCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    _customer_or_404(db, customer_id)
    material = db.get(Material, payload.material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=400, detail="所选材质不存在或已停用")
    _require_active_material_supplier(db, material)
    flute = _validated_preference_flute(material, payload.flute_type)
    preference = CustomerQuotePreference(
        customer_id=customer_id,
        box_type=_normalized_quote_box_type(payload.box_type),
        material_id=material.id,
        flute_type=flute,
        tax_included_square_price=payload.tax_included_square_price,
        is_active=payload.is_active,
    )
    try:
        db.add(preference)
        db.flush()
        audit_master_change(
            db,
            user=user,
            action="CREATE",
            resource="CustomerQuotePreference",
            resource_id=preference.id,
            details={"after": _quote_preference_response(preference)},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="该客户、箱型、材质和楞型的报价偏好已存在",
        ) from error
    db.refresh(preference)
    return _quote_preference_response(preference)


@router.put("/{customer_id}/quote-preferences/{preference_id}")
def update_customer_quote_preference(
    customer_id: int,
    preference_id: int,
    payload: CustomerQuotePreferenceUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    preference = _quote_preference_or_404(db, customer_id, preference_id)
    if preference.version != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"客户报价偏好已从 v{payload.expected_version} 更新为 "
                f"v{preference.version}，请刷新后再保存"
            ),
        )
    before = _quote_preference_response(preference)
    updates = {
        "tax_included_square_price": payload.tax_included_square_price,
        "is_active": payload.is_active,
    }
    changed = {
        key: value for key, value in updates.items() if getattr(preference, key) != value
    }
    if changed:
        for key, value in changed.items():
            setattr(preference, key, value)
        preference.version += 1
        db.flush()
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="CustomerQuotePreference",
            resource_id=preference.id,
            details={
                "before": before,
                "after": _quote_preference_response(preference),
                "change_reason": "修改客户尺寸报价偏好",
            },
        )
        db.commit()
    db.refresh(preference)
    return _quote_preference_response(preference)


@router.post("/{customer_id}/quote-preferences/estimate")
def estimate_customer_quote_preference(
    customer_id: int,
    payload: CustomerQuoteEstimatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    _customer_or_404(db, customer_id)
    material = db.get(Material, payload.material_id)
    if material is None or not material.is_active:
        raise HTTPException(status_code=400, detail="所选材质不存在或已停用")
    _require_active_material_supplier(db, material)
    flute = _validated_preference_flute(material, payload.flute_type)
    box_type = _require_a1_quote_box_type(payload.box_type)
    preference = db.scalar(
        select(CustomerQuotePreference)
        .where(
            CustomerQuotePreference.customer_id == customer_id,
            CustomerQuotePreference.box_type == box_type,
            CustomerQuotePreference.material_id == material.id,
            CustomerQuotePreference.flute_type == flute,
            CustomerQuotePreference.is_active.is_(True),
        )
        .order_by(CustomerQuotePreference.id.desc())
    )
    try:
        square = resolve_customer_square_price(
            db,
            material=material,
            flute_type=flute,
            saved_square_price=(
                preference.tax_included_square_price if preference is not None else None
            ),
        )
        estimate = estimate_a1_unit_price(
            length_mm=payload.length_mm,
            width_mm=payload.width_mm,
            height_mm=payload.height_mm,
            customer_square_price=square["customer_square_price"],
            manual_unit_price=payload.manual_unit_price,
        )
    except CustomerQuotePricingError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    response = {
        "customer_id": customer_id,
        "box_type": box_type,
        "material_id": material.id,
        "flute_type": flute,
        "preference_id": preference.id if preference is not None else None,
        **square,
        **estimate,
    }
    if not has_permission(user, "cost.view"):
        response["material_effective_square_price"] = None
    return response


@router.post("", status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> CustomerResponse:
    data = _customer_write_data(payload)
    customer = Customer(**data)
    try:
        db.add(customer)
        db.flush()
        # A newly created customer must be usable immediately by the sales
        # account that created it.  Keep this in the same transaction as the
        # customer and audit rows so failed creates cannot leave a scope row.
        if not has_unrestricted_customer_access(user, db):
            db.add(
                UserCustomerScope(
                    user_id=user.id,
                    customer_id=customer.id,
                    assigned_by=user.id,
                )
            )
        record_versioned_create(
            db,
            object_type="customer",
            entity=customer,
            user=user,
            reason="新增客户",
            source="api.customers.create",
        )
        audit_master_change(
            db,
            user=user,
            action="CREATE",
            resource="Customer",
            resource_id=customer.id,
            details=payload.model_dump(),
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="客户编号、缩写或名称重复") from error
    except Exception:
        db.rollback()
        raise
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.post("/{customer_id}/update-preview")
def preview_customer_update(
    customer_id: int,
    payload: CustomerUpdatePreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = _customer_or_404(db, customer_id)
    updates = _customer_write_data(payload, existing=customer)
    return preview_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates=updates,
        expected_version=payload.expected_version,
        user=user,
    )


@router.put("/{customer_id}")
def update_customer(
    customer_id: int,
    payload: CustomerUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> CustomerResponse:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = _customer_or_404(db, customer_id)
    before = CustomerResponse.model_validate(customer).model_dump()
    updates = _customer_write_data(payload, existing=customer)
    changed = _changed_updates(customer, updates)
    try:
        apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
            source="api.customers.update",
            confirmation_token=payload.confirmation_token,
        )
        if changed:
            audit_master_change(
                db,
                user=user,
                action="UPDATE",
                resource="Customer",
                resource_id=customer.id,
                details={"before": before, "after": updates},
            )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="客户编号、缩写或名称重复") from error
    except Exception:
        db.rollback()
        raise
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.put("/{customer_id}/status")
def update_customer_status(
    customer_id: int,
    payload: CustomerStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_deactivate),
) -> CustomerResponse:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = _customer_or_404(db, customer_id)
    if not payload.is_active:
        open_order = db.scalar(
            select(Order.id)
            .where(
                Order.customer_id == customer.id,
                not_(
                    or_(
                        Order.status == "cancelled",
                        and_(
                            Order.status == "delivered",
                            Order.payment_status == "paid",
                        ),
                    )
                ),
            )
            .limit(1)
        )
        if open_order is not None:
            raise HTTPException(
                status_code=400,
                detail="客户存在未结案订单，不能停用",
            )
    before = customer.is_active
    updates = {
        "is_active": payload.is_active,
        "status": "active" if payload.is_active else "inactive",
    }
    changed = _changed_updates(customer, updates)
    try:
        apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
            source="api.customers.status",
            action="status_change",
            confirmation_token=payload.confirmation_token,
        )
        if changed:
            audit_master_change(
                db,
                user=user,
                action="ENABLE" if payload.is_active else "DISABLE",
                resource="Customer",
                resource_id=customer.id,
                details={"before": before, "after": payload.is_active},
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: int,
    payload: CustomerMutationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_delete),
) -> Response:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = _customer_or_404(db, customer_id)
    open_order = db.scalar(
        select(Order.id)
        .where(
            Order.customer_id == customer.id,
            not_(
                or_(
                    Order.status == "cancelled",
                    and_(
                        Order.status == "delivered",
                        Order.payment_status == "paid",
                    ),
                )
            ),
        )
        .limit(1)
    )
    if open_order is not None:
        raise HTTPException(status_code=400, detail="客户存在未结案订单，不能删除")
    updates = {"is_active": False, "status": "inactive"}
    changed = _changed_updates(customer, updates)
    try:
        apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
            source="api.customers.delete",
            action="soft_delete",
            confirmation_token=payload.confirmation_token,
        )
        if changed:
            audit_master_change(
                db,
                user=user,
                action="DELETE",
                resource="Customer",
                resource_id=customer.id,
                details={
                    "name": customer.name,
                    "customer_code": customer.customer_code,
                },
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)
