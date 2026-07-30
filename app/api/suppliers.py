from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import PermissionChecker, RoleChecker, get_db
from app.api.master_data_common import audit_master_change
from app.models.supplier import Supplier, SupplierAlias
from app.models.user import User
from app.services.supplier_master import (
    clean_business_code,
    clean_supplier_name,
    normalize_supplier_identity,
    supplier_snapshot,
)


router = APIRouter()
can_read_candidates = PermissionChecker("products.view")
admin_only = RoleChecker(["admin"])


class SupplierPayload(BaseModel):
    standard_name: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)
    business_code: str | None = Field(default=None, max_length=50)
    contact_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=100)
    remarks: str | None = None
    sort_order: int = Field(default=100, ge=0, le=100_000)
    aliases: list[str] = Field(default_factory=list, max_length=30)

    @field_validator(
        "standard_name",
        "display_name",
        "business_code",
        "contact_name",
        "phone",
        "remarks",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("aliases", mode="before")
    @classmethod
    def clean_aliases(cls, value: Any) -> list[str]:
        if value is None:
            return []
        return [str(item).strip() for item in value if str(item).strip()]


class SupplierUpdatePayload(SupplierPayload):
    expected_version: int = Field(ge=1)


class SupplierStatusPayload(BaseModel):
    expected_version: int = Field(ge=1)
    is_active: bool


def _supplier_or_404(db: Session, supplier_id: int) -> Supplier:
    supplier = db.scalar(
        select(Supplier)
        .options(selectinload(Supplier.aliases))
        .where(Supplier.id == supplier_id)
    )
    if supplier is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return supplier


def _version_conflict(supplier: Supplier, expected_version: int) -> None:
    if supplier.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"供应商资料已从 v{expected_version} 更新为 "
                f"v{supplier.version}，请刷新后再保存"
            ),
        )


def _atomic_supplier_update(
    db: Session,
    *,
    supplier: Supplier,
    expected_version: int,
    values: dict[str, Any],
) -> None:
    try:
        result = db.execute(
            update(Supplier)
            .where(
                Supplier.id == supplier.id,
                Supplier.version == expected_version,
            )
            .values(**values, version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="供应商名称、别名或非空业务代码与现有资料冲突",
        ) from error
    if result.rowcount != 1:
        db.rollback()
        current = _supplier_or_404(db, supplier.id)
        _version_conflict(current, expected_version)
        raise HTTPException(status_code=409, detail="供应商资料版本冲突")
    db.refresh(supplier)


def _clean_payload(payload: SupplierPayload) -> tuple[dict, list[tuple[str, str]]]:
    standard_name = clean_supplier_name(payload.standard_name)
    normalized_name = normalize_supplier_identity(standard_name)
    display_name = clean_supplier_name(payload.display_name) or None
    business_code = clean_business_code(payload.business_code)
    aliases: list[tuple[str, str]] = []
    seen = {normalized_name}
    for raw in payload.aliases:
        alias_name = clean_supplier_name(raw)
        normalized_alias = normalize_supplier_identity(alias_name)
        if not normalized_alias or normalized_alias in seen:
            continue
        seen.add(normalized_alias)
        aliases.append((alias_name, normalized_alias))
    return (
        {
            "standard_name": standard_name,
            "normalized_name": normalized_name,
            "display_name": display_name,
            "business_code": business_code,
            "normalized_business_code": business_code,
            "contact_name": clean_supplier_name(payload.contact_name) or None,
            "phone": clean_supplier_name(payload.phone) or None,
            "remarks": clean_supplier_name(payload.remarks) or None,
            "sort_order": payload.sort_order,
        },
        aliases,
    )


def _raise_identity_conflicts(
    db: Session,
    *,
    supplier_id: int | None,
    values: dict,
    aliases: list[tuple[str, str]],
) -> None:
    own_id = supplier_id if supplier_id is not None else -1
    requested_names = {values["normalized_name"], *(item[1] for item in aliases)}
    name_conflict = db.scalar(
        select(Supplier.id).where(
            Supplier.id != own_id,
            Supplier.normalized_name.in_(requested_names),
        )
    )
    alias_conflict = db.scalar(
        select(SupplierAlias.supplier_id).where(
            SupplierAlias.supplier_id != own_id,
            SupplierAlias.normalized_alias.in_(requested_names),
        )
    )
    if name_conflict is not None or alias_conflict is not None:
        raise HTTPException(
            status_code=409,
            detail="供应商标准名称或别名已被其他供应商使用",
        )
    business_code = values["normalized_business_code"]
    if business_code:
        code_conflict = db.scalar(
            select(Supplier.id).where(
                Supplier.id != own_id,
                Supplier.normalized_business_code == business_code,
            )
        )
        if code_conflict is not None:
            raise HTTPException(
                status_code=409,
                detail=f"供应商业务代码 {business_code} 已被使用",
            )


def _set_aliases(supplier: Supplier, aliases: list[tuple[str, str]]) -> None:
    target = {normalized: name for name, normalized in aliases}
    for existing in list(supplier.aliases):
        if existing.normalized_alias not in target:
            supplier.aliases.remove(existing)
        else:
            existing.alias_name = target.pop(existing.normalized_alias)
    supplier.aliases.extend(
        SupplierAlias(alias_name=name, normalized_alias=normalized)
        for normalized, name in target.items()
    )


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="供应商名称、别名或非空业务代码与现有资料冲突",
        ) from error


def _flush_or_conflict(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="供应商名称、别名或非空业务代码与现有资料冲突",
        ) from error


@router.get("/candidates")
def list_supplier_candidates(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read_candidates),
) -> dict:
    rows = db.scalars(
        select(Supplier)
        .options(selectinload(Supplier.aliases))
        .where(Supplier.is_active.is_(True))
        .order_by(
            Supplier.sort_order,
            Supplier.display_name,
            Supplier.standard_name,
            Supplier.id,
        )
    ).all()
    return {"items": [supplier_snapshot(row) for row in rows]}


@router.get("")
def list_suppliers(
    keyword: str = Query(default=""),
    include_inactive: bool = Query(default=True),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    statement = select(Supplier).options(selectinload(Supplier.aliases))
    if not include_inactive:
        statement = statement.where(Supplier.is_active.is_(True))
    cleaned_keyword = keyword.strip()
    if cleaned_keyword:
        pattern = f"%{cleaned_keyword}%"
        statement = (
            statement.outerjoin(SupplierAlias)
            .where(
                or_(
                    Supplier.standard_name.ilike(pattern),
                    Supplier.display_name.ilike(pattern),
                    Supplier.business_code.ilike(pattern),
                    SupplierAlias.alias_name.ilike(pattern),
                )
            )
            .distinct()
        )
    rows = db.scalars(
        statement.order_by(
            Supplier.is_active.desc(),
            Supplier.sort_order,
            Supplier.display_name,
            Supplier.standard_name,
            Supplier.id,
        )
    ).all()
    return {"items": [supplier_snapshot(row) for row in rows]}


@router.get("/{supplier_id}")
def get_supplier(
    supplier_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    return supplier_snapshot(_supplier_or_404(db, supplier_id))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    values, aliases = _clean_payload(payload)
    _raise_identity_conflicts(
        db,
        supplier_id=None,
        values=values,
        aliases=aliases,
    )
    supplier = Supplier(**values, is_active=True, version=1)
    _set_aliases(supplier, aliases)
    db.add(supplier)
    _flush_or_conflict(db)
    after = supplier_snapshot(supplier)
    audit_master_change(
        db,
        user=user,
        action="CREATE",
        resource="Supplier",
        resource_id=supplier.id,
        details={"after": after},
    )
    _commit_or_conflict(db)
    db.refresh(supplier)
    return supplier_snapshot(_supplier_or_404(db, supplier.id))


@router.put("/{supplier_id}")
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    _version_conflict(supplier, payload.expected_version)
    before = supplier_snapshot(supplier)
    values, aliases = _clean_payload(payload)
    if values["normalized_name"] != supplier.normalized_name:
        old_normalized = supplier.normalized_name
        if old_normalized not in {item[1] for item in aliases}:
            aliases.append((supplier.standard_name, old_normalized))
    _raise_identity_conflicts(
        db,
        supplier_id=supplier.id,
        values=values,
        aliases=aliases,
    )
    changed = any(getattr(supplier, key) != value for key, value in values.items())
    current_aliases = [
        (row.alias_name, row.normalized_alias) for row in supplier.aliases
    ]
    if current_aliases != aliases:
        changed = True
    if not changed:
        return before
    _atomic_supplier_update(
        db,
        supplier=supplier,
        expected_version=payload.expected_version,
        values=values,
    )
    _set_aliases(supplier, aliases)
    _flush_or_conflict(db)
    after = supplier_snapshot(supplier)
    audit_master_change(
        db,
        user=user,
        action="UPDATE",
        resource="Supplier",
        resource_id=supplier.id,
        details={"before": before, "after": after},
    )
    _commit_or_conflict(db)
    return supplier_snapshot(_supplier_or_404(db, supplier.id))


@router.put("/{supplier_id}/status")
def update_supplier_status(
    supplier_id: int,
    payload: SupplierStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    supplier = _supplier_or_404(db, supplier_id)
    _version_conflict(supplier, payload.expected_version)
    if supplier.is_active == payload.is_active:
        return supplier_snapshot(supplier)
    before = supplier_snapshot(supplier)
    _atomic_supplier_update(
        db,
        supplier=supplier,
        expected_version=payload.expected_version,
        values={"is_active": payload.is_active},
    )
    _flush_or_conflict(db)
    after = supplier_snapshot(supplier)
    audit_master_change(
        db,
        user=user,
        action="ACTIVATE" if payload.is_active else "DEACTIVATE",
        resource="Supplier",
        resource_id=supplier.id,
        details={"before": before, "after": after},
    )
    _commit_or_conflict(db)
    return supplier_snapshot(_supplier_or_404(db, supplier.id))
