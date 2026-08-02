from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    RoleChecker,
    get_current_user,
    get_db,
    has_permission,
    require_customer_access,
)
from app.models.customer import Customer
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.material import Material
from app.models.product import Product
from app.models.user import User
from app.services.master_data_versioning import (
    apply_versioned_update,
    get_object_version,
    list_object_versions,
    preview_versioned_restore,
    revision_snapshot,
    serialize_revision,
    snapshot_updates,
)


router = APIRouter()
admin_only = RoleChecker(["admin"])
ObjectType = Literal["customer", "product", "material"]
_PRODUCT_PRICE_HISTORY_FIELDS = frozenset(
    {
        "sale_unit_price",
        "sale_unit_price_no_tax",
        "cost_unit_price",
        "board_price",
        "suggested_price",
    }
)
_PRODUCT_COST_HISTORY_FIELDS = frozenset(
    {"cost_unit_price", "board_price", "suggested_price"}
)
_MATERIAL_SENSITIVE_HISTORY_FIELDS = frozenset(
    {
        "quote_price",
        "rule_base_price",
        "price_source",
        "price_unit",
        "supplier_name",
        "quote_date",
    }
)
_MATERIAL_WORKSHOP_HIDDEN_HISTORY_FIELDS = (
    _MATERIAL_SENSITIVE_HISTORY_FIELDS | frozenset({"remarks"})
)


class RestorePreviewPayload(BaseModel):
    expected_version: int = Field(ge=1)


class RestorePayload(RestorePreviewPayload):
    reason: str | None = Field(default=None, max_length=500)
    confirmation_token: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_optional_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None


def _entity_or_404(
    db: Session,
    *,
    object_type: ObjectType,
    object_id: int,
) -> Customer | Product | Material:
    models = {
        "customer": Customer,
        "product": Product,
        "material": Material,
    }
    entity = db.get(models[object_type], object_id)
    if entity is None:
        names = {"customer": "客户", "product": "产品", "material": "材质"}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{names[object_type]}不存在",
        )
    return entity


def _require_view_access(
    db: Session,
    *,
    object_type: ObjectType,
    entity: Customer | Product | Material,
    user: User,
) -> None:
    permission = "customers.view" if object_type == "customer" else "products.view"
    if not has_permission(user, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足",
        )
    if object_type == "customer":
        require_customer_access(int(entity.id), current_user=user, db=db)
    elif object_type == "product":
        require_customer_access(
            int(entity.customer_id),
            current_user=user,
            db=db,
        )


def _entity_with_access(
    db: Session,
    *,
    object_type: ObjectType,
    object_id: int,
    user: User,
) -> Customer | Product | Material:
    entity = _entity_or_404(
        db,
        object_type=object_type,
        object_id=object_id,
    )
    _require_view_access(
        db,
        object_type=object_type,
        entity=entity,
        user=user,
    )
    return entity


def _revision_or_404(
    db: Session,
    *,
    object_type: ObjectType,
    object_id: int,
    version: int,
) -> MasterDataObjectVersion:
    revision = get_object_version(
        db,
        object_type=object_type,
        object_id=object_id,
        version=version,
    )
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定历史版本不存在",
        )
    return revision


def _integrity_error_detail(error: IntegrityError) -> str:
    message = str(error.orig).lower()
    if "foreign key" in message:
        return "恢复失败：目标版本引用的客户、材质或模具等基础资料已不存在"
    if "unique" in message or "duplicate" in message:
        return "恢复失败：目标版本会造成编码、料号或名称重复，请先处理冲突"
    return "恢复失败：目标版本与当前主数据约束冲突"


def _visible_revision_payload(
    payload: dict,
    *,
    object_type: ObjectType,
    user: User,
) -> dict:
    """Remove cost/supplier-price values at the API boundary.

    The append-only ledger must retain a complete admin recovery snapshot, but
    a sales account with product view permission must not gain cost visibility
    through the new history endpoint.
    """

    if object_type == "product":
        hidden = (
            _PRODUCT_PRICE_HISTORY_FIELDS
            if user.role == "workshop"
            else (
                frozenset()
                if has_permission(user, "cost.view")
                else _PRODUCT_COST_HISTORY_FIELDS
            )
        )
    elif object_type == "material":
        hidden = (
            _MATERIAL_WORKSHOP_HIDDEN_HISTORY_FIELDS
            if user.role == "workshop"
            else (
                frozenset()
                if has_permission(user, "cost.view")
                else _MATERIAL_SENSITIVE_HISTORY_FIELDS
            )
        )
    else:
        hidden = frozenset()
    if not hidden:
        return payload
    visible = dict(payload)
    for key in ("changed_fields", "snapshot"):
        values = visible.get(key)
        if isinstance(values, dict):
            visible[key] = {
                field: value for field, value in values.items() if field not in hidden
            }
    return visible


@router.get("/{object_type}/{object_id}/versions")
def list_versions(
    object_type: ObjectType,
    object_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    entity = _entity_with_access(
        db,
        object_type=object_type,
        object_id=object_id,
        user=user,
    )
    revisions = list_object_versions(
        db,
        object_type=object_type,
        object_id=object_id,
        offset=offset,
        limit=limit,
    )
    total = int(
        db.scalar(
            select(func.count())
            .select_from(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == object_type,
                MasterDataObjectVersion.object_id == object_id,
            )
        )
        or 0
    )
    return {
        "object_type": object_type,
        "object_id": object_id,
        "current_version": entity.version,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            _visible_revision_payload(
                serialize_revision(item), object_type=object_type, user=user
            )
            for item in revisions
        ],
    }


@router.get("/{object_type}/{object_id}/versions/{version}")
def get_version_detail(
    object_type: ObjectType,
    object_id: int,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    _entity_with_access(
        db,
        object_type=object_type,
        object_id=object_id,
        user=user,
    )
    revision = _revision_or_404(
        db,
        object_type=object_type,
        object_id=object_id,
        version=version,
    )
    try:
        snapshot = revision_snapshot(revision)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return _visible_revision_payload(
        {**serialize_revision(revision), "snapshot": snapshot},
        object_type=object_type,
        user=user,
    )


@router.post("/{object_type}/{object_id}/versions/{version}/restore-preview")
def restore_preview(
    object_type: ObjectType,
    object_id: int,
    version: int,
    payload: RestorePreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    entity = _entity_with_access(
        db,
        object_type=object_type,
        object_id=object_id,
        user=user,
    )
    revision = _revision_or_404(
        db,
        object_type=object_type,
        object_id=object_id,
        version=version,
    )
    try:
        return preview_versioned_restore(
            db,
            object_type=object_type,
            entity=entity,
            revision=revision,
            expected_version=payload.expected_version,
            user=user,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.post("/{object_type}/{object_id}/versions/{version}/restore")
def restore_version(
    object_type: ObjectType,
    object_id: int,
    version: int,
    payload: RestorePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    entity = _entity_with_access(
        db,
        object_type=object_type,
        object_id=object_id,
        user=user,
    )
    target = _revision_or_404(
        db,
        object_type=object_type,
        object_id=object_id,
        version=version,
    )
    try:
        updates = snapshot_updates(object_type, target)
        restored = apply_versioned_update(
            db,
            object_type=object_type,
            entity=entity,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.reason or "系统记录：恢复主数据历史版本",
            source="api.master_data.restore",
            action="restore",
            confirmation_token=payload.confirmation_token,
            restored_from_version=target.version,
        )
        if restored is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "MASTER_RESTORE_NO_CHANGES",
                    "message": "当前数据已与目标历史版本一致，无需恢复",
                },
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_integrity_error_detail(error),
        ) from error
    except ValueError as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except Exception:
        db.rollback()
        raise

    db.refresh(entity)
    db.refresh(restored)
    return {
        "object_type": object_type,
        "object_id": object_id,
        "version": entity.version,
        "restored_from_version": target.version,
        "revision": serialize_revision(restored),
    }
