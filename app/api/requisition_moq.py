from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db
from app.api.master_data_common import audit_master_change
from app.models.customer import Customer
from app.models.supplier import Supplier
from app.models.supplier_moq import SupplierMinimumOrderRule
from app.models.user import User


router = APIRouter()
can_configure = PermissionChecker("requisition.config")

ScopeType = Literal["supplier", "customer", "material", "flute"]
MoqUnit = Literal["sheets", "square_meters", "meters", "amount", "group_total"]
RuleStatus = Literal["active", "inactive"]


class RuleFields(BaseModel):
    supplier_id: int = Field(gt=0)
    scope_type: ScopeType
    scope_value: str | None = None
    minimum_quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unit: MoqUnit
    merge_allowed: bool = False
    merge_window_days: int | None = Field(default=None, ge=1, le=365)
    effective_from: date
    effective_to: date | None = None
    evidence_reference: str = Field(min_length=1, max_length=1000)

    @field_validator("minimum_quantity", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value):
        if isinstance(value, bool):
            raise ValueError("MOQ 数量必须是正数")
        return value

    @field_validator("scope_value", "evidence_reference", mode="before")
    @classmethod
    def trim_optional_text(cls, value):
        if value is None:
            return None
        return str(value).strip()

    @model_validator(mode="after")
    def validate_contract(self):
        if self.scope_type == "supplier" and self.scope_value:
            raise ValueError("供应商全局范围不能填写范围值")
        if self.scope_type != "supplier" and not self.scope_value:
            raise ValueError("客户、材质或楞型范围必须填写范围值")
        if self.merge_allowed and self.merge_window_days is None:
            raise ValueError("允许合并时必须填写合并窗口")
        if not self.merge_allowed and self.merge_window_days is not None:
            raise ValueError("不允许合并时不能填写合并窗口")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("结束日期不能早于开始日期")
        return self


class CreateRulePayload(RuleFields):
    pass


class UpdateRulePayload(RuleFields):
    expected_version: int = Field(gt=0)


class StatusPayload(BaseModel):
    expected_version: int = Field(gt=0)
    status: RuleStatus


def _display_supplier(supplier: Supplier) -> str:
    return supplier.display_name or supplier.standard_name


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _snapshot(rule: SupplierMinimumOrderRule) -> dict:
    return {
        "id": rule.id,
        "supplier_id": rule.supplier_id,
        "supplier_name": rule.supplier_name_snapshot,
        "supplier_name_snapshot": rule.supplier_name_snapshot,
        "scope_type": rule.scope_type,
        "scope_value": rule.scope_value,
        "scope_label": rule.scope_label_snapshot,
        "scope_label_snapshot": rule.scope_label_snapshot,
        "minimum_quantity": _decimal_text(rule.minimum_quantity),
        "unit": rule.unit,
        "merge_allowed": rule.merge_allowed,
        "merge_window_days": rule.merge_window_days,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
        "status": rule.status,
        "source": rule.source,
        "evidence_reference": rule.evidence_reference,
        "confirmed_by_user_id": rule.confirmed_by_user_id,
        "confirmed_by_username": rule.confirmed_by_username_snapshot,
        "confirmed_at": rule.confirmed_at.isoformat() if rule.confirmed_at else None,
        "version": rule.version,
    }


def _rule_or_404(db: Session, rule_id: int) -> SupplierMinimumOrderRule:
    rule = db.get(SupplierMinimumOrderRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="供应商 MOQ 规则不存在")
    return rule


def _active_supplier_or_error(db: Session, supplier_id: int) -> Supplier:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=422, detail="供应商不存在")
    if not supplier.is_active:
        raise HTTPException(status_code=409, detail="停用供应商不能维护生效 MOQ 规则")
    return supplier


def _normalize_scope(
    db: Session, scope_type: str, scope_value: str | None
) -> tuple[str | None, str]:
    if scope_type == "supplier":
        return None, "全部"
    cleaned = str(scope_value or "").strip()
    if scope_type == "customer":
        try:
            customer_id = int(cleaned)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="客户范围必须使用客户 ID") from error
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.is_active:
            raise HTTPException(status_code=422, detail="客户范围不存在或已停用")
        return str(customer.id), customer.name
    normalized = " ".join(cleaned.upper().split())
    if not normalized:
        raise HTTPException(status_code=422, detail="材质或楞型范围不能为空")
    return normalized, normalized


def _claim_supplier(db: Session, supplier: Supplier) -> None:
    result = db.execute(
        update(Supplier)
        .where(Supplier.id == supplier.id, Supplier.version == supplier.version)
        .values(version=Supplier.version)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="供应商版本已变化，请刷新后重试")
    db.flush()


def _raise_overlap(
    db: Session,
    *,
    supplier_id: int,
    scope_type: str,
    scope_value: str | None,
    unit: str,
    effective_from: date,
    effective_to: date | None,
    exclude_id: int | None = None,
) -> None:
    end = effective_to or date.max
    filters = [
        SupplierMinimumOrderRule.supplier_id == supplier_id,
        SupplierMinimumOrderRule.scope_type == scope_type,
        SupplierMinimumOrderRule.unit == unit,
        SupplierMinimumOrderRule.status == "active",
        SupplierMinimumOrderRule.effective_from <= end,
        or_(
            SupplierMinimumOrderRule.effective_to.is_(None),
            SupplierMinimumOrderRule.effective_to >= effective_from,
        ),
    ]
    if scope_value is None:
        filters.append(SupplierMinimumOrderRule.scope_value.is_(None))
    else:
        filters.append(SupplierMinimumOrderRule.scope_value == scope_value)
    if exclude_id is not None:
        filters.append(SupplierMinimumOrderRule.id != exclude_id)
    if db.scalar(select(SupplierMinimumOrderRule.id).where(and_(*filters)).limit(1)):
        raise HTTPException(status_code=409, detail="同供应商、范围和单位的生效区间重叠")


def _values(db: Session, payload: RuleFields, supplier: Supplier, user: User) -> dict:
    scope_value, scope_label = _normalize_scope(
        db, payload.scope_type, payload.scope_value
    )
    return {
        "supplier_id": supplier.id,
        "supplier_name_snapshot": _display_supplier(supplier),
        "scope_type": payload.scope_type,
        "scope_value": scope_value,
        "scope_label_snapshot": scope_label,
        "minimum_quantity": payload.minimum_quantity,
        "unit": payload.unit,
        "merge_allowed": payload.merge_allowed,
        "merge_window_days": payload.merge_window_days,
        "effective_from": payload.effective_from,
        "effective_to": payload.effective_to,
        "evidence_reference": payload.evidence_reference.strip(),
        "confirmed_by_user_id": user.id,
        "confirmed_by_username_snapshot": user.username,
        "confirmed_at": datetime.now(),
        "updated_by_user_id": user.id,
    }


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="MOQ 规则并发冲突，请刷新后重试") from error


@router.get("")
def list_rules(
    supplier_id: int | None = None,
    scope_type: ScopeType | None = None,
    status_filter: RuleStatus | None = None,
    status: RuleStatus | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(can_configure),
) -> dict:
    statement = select(SupplierMinimumOrderRule)
    if supplier_id is not None:
        statement = statement.where(SupplierMinimumOrderRule.supplier_id == supplier_id)
    if scope_type is not None:
        statement = statement.where(SupplierMinimumOrderRule.scope_type == scope_type)
    selected_status = status_filter or status
    if selected_status is not None:
        statement = statement.where(SupplierMinimumOrderRule.status == selected_status)
    rules = list(
        db.scalars(
            statement.order_by(
                SupplierMinimumOrderRule.supplier_name_snapshot,
                SupplierMinimumOrderRule.scope_type,
                SupplierMinimumOrderRule.effective_from.desc(),
                SupplierMinimumOrderRule.id.desc(),
            )
        )
    )
    suppliers = list(
        db.scalars(
            select(Supplier)
            .where(Supplier.is_active.is_(True))
            .order_by(Supplier.sort_order, Supplier.display_name, Supplier.standard_name)
        )
    )
    return {
        "items": [_snapshot(rule) for rule in rules],
        "total": len(rules),
        "suppliers": [
            {"id": supplier.id, "name": _display_supplier(supplier)}
            for supplier in suppliers
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: CreateRulePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_configure),
) -> dict:
    supplier = _active_supplier_or_error(db, payload.supplier_id)
    _claim_supplier(db, supplier)
    values = _values(db, payload, supplier, user)
    _raise_overlap(db, **{key: values[key] for key in (
        "supplier_id", "scope_type", "scope_value", "unit", "effective_from", "effective_to"
    )})
    rule = SupplierMinimumOrderRule(
        **values,
        status="active",
        source="manual_confirmation",
        created_by_user_id=user.id,
        version=1,
    )
    db.add(rule)
    db.flush()
    after = _snapshot(rule)
    audit_master_change(
        db, user=user, action="CREATE", resource="SupplierMinimumOrderRule",
        resource_id=rule.id, details={"after": after},
    )
    _commit(db)
    return _snapshot(_rule_or_404(db, rule.id))


@router.put("/{rule_id}")
def update_rule(
    rule_id: int,
    payload: UpdateRulePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_configure),
) -> dict:
    rule = _rule_or_404(db, rule_id)
    if rule.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="MOQ 规则版本已变化，请刷新后重试")
    supplier = _active_supplier_or_error(db, payload.supplier_id)
    _claim_supplier(db, supplier)
    values = _values(db, payload, supplier, user)
    if rule.status == "active":
        _raise_overlap(db, exclude_id=rule.id, **{key: values[key] for key in (
            "supplier_id", "scope_type", "scope_value", "unit", "effective_from", "effective_to"
        )})
    before = _snapshot(rule)
    result = db.execute(
        update(SupplierMinimumOrderRule)
        .where(
            SupplierMinimumOrderRule.id == rule.id,
            SupplierMinimumOrderRule.version == payload.expected_version,
        )
        .values(**values, version=payload.expected_version + 1, updated_at=func.current_timestamp())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="MOQ 规则版本已变化，请刷新后重试")
    db.flush()
    db.expire_all()
    updated = _rule_or_404(db, rule.id)
    after = _snapshot(updated)
    audit_master_change(
        db, user=user, action="UPDATE", resource="SupplierMinimumOrderRule",
        resource_id=updated.id, details={"before": before, "after": after},
    )
    _commit(db)
    return _snapshot(_rule_or_404(db, rule.id))


@router.put("/{rule_id}/status")
def update_rule_status(
    rule_id: int,
    payload: StatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_configure),
) -> dict:
    rule = _rule_or_404(db, rule_id)
    if rule.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="MOQ 规则版本已变化，请刷新后重试")
    supplier = db.get(Supplier, rule.supplier_id)
    if supplier is None:
        raise HTTPException(status_code=422, detail="供应商不存在")
    if payload.status == "active" and not supplier.is_active:
        raise HTTPException(status_code=409, detail="停用供应商不能恢复 MOQ 规则")
    _claim_supplier(db, supplier)
    if payload.status == "active":
        _raise_overlap(
            db,
            supplier_id=rule.supplier_id,
            scope_type=rule.scope_type,
            scope_value=rule.scope_value,
            unit=rule.unit,
            effective_from=rule.effective_from,
            effective_to=rule.effective_to,
            exclude_id=rule.id,
        )
    before = _snapshot(rule)
    result = db.execute(
        update(SupplierMinimumOrderRule)
        .where(
            SupplierMinimumOrderRule.id == rule.id,
            SupplierMinimumOrderRule.version == payload.expected_version,
        )
        .values(
            status=payload.status,
            version=payload.expected_version + 1,
            updated_by_user_id=user.id,
            confirmed_by_user_id=user.id,
            confirmed_by_username_snapshot=user.username,
            confirmed_at=datetime.now(),
            updated_at=func.current_timestamp(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="MOQ 规则版本已变化，请刷新后重试")
    db.flush()
    db.expire_all()
    updated = _rule_or_404(db, rule.id)
    after = _snapshot(updated)
    audit_master_change(
        db, user=user, action="STATUS", resource="SupplierMinimumOrderRule",
        resource_id=updated.id, details={"before": before, "after": after},
    )
    _commit(db)
    return _snapshot(_rule_or_404(db, rule.id))
