from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
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
from app.models.order import Order
from app.models.user import User


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


class CustomerStatusPayload(BaseModel):
    is_active: bool


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
    data = payload.model_dump()
    data.update(
        customer_code=clean_code(payload.customer_code),
        name=payload.name.strip(),
        is_active=payload.status == "active",
    )
    customer = Customer(**data)
    try:
        db.add(customer)
        db.flush()
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
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.put("/{customer_id}")
def update_customer(
    customer_id: int,
    payload: CustomerPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> CustomerResponse:
    require_customer_access(customer_id, current_user=user, db=db)
    customer = _customer_or_404(db, customer_id)
    before = CustomerResponse.model_validate(customer).model_dump()
    for key, value in payload.model_dump().items():
        setattr(customer, key, value)
    customer.customer_code = clean_code(payload.customer_code)
    customer.name = payload.name.strip()
    customer.is_active = payload.status == "active"
    try:
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="Customer",
            resource_id=customer.id,
            details={"before": before, "after": payload.model_dump()},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="客户编号、缩写或名称重复") from error
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
    customer.is_active = payload.is_active
    customer.status = "active" if payload.is_active else "inactive"
    audit_master_change(
        db,
        user=user,
        action="ENABLE" if payload.is_active else "DISABLE",
        resource="Customer",
        resource_id=customer.id,
        details={"before": before, "after": payload.is_active},
    )
    db.commit()
    db.refresh(customer)
    return CustomerResponse.model_validate(customer)


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: int,
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
    audit_master_change(
        db,
        user=user,
        action="DELETE",
        resource="Customer",
        resource_id=customer.id,
        details={"name": customer.name, "customer_code": customer.customer_code},
    )
    customer.is_active = False
    customer.status = "inactive"
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
