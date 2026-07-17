from __future__ import annotations

from decimal import Decimal
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
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.api.master_data_common import audit_master_change, clean_code
from app.models.customer import Customer
from app.models.access_control import UserCustomerScope
from app.models.order import Order
from app.models.user import User
from app.services.master_data_versioning import (
    apply_versioned_update,
    record_versioned_create,
)


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


class CustomerResponse(CustomerPayload):
    model_config = ConfigDict(from_attributes=True)

    id: int
    is_active: bool
    version: int


class CustomerMutationPayload(BaseModel):
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


class CustomerUpdatePayload(CustomerPayload):
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


class CustomerStatusPayload(CustomerMutationPayload):
    is_active: bool


def _customer_write_data(payload: CustomerPayload) -> dict:
    data = payload.model_dump(include=set(CustomerPayload.model_fields))
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
    updates = _customer_write_data(payload)
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
