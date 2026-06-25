from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.api.master_data_common import audit_master_change, clean_code
from app.models.material import Material
from app.models.user import User
from app.services.flute_mapping import validate_flute_consistency


router = APIRouter()
can_read = RoleChecker(["admin", "sales", "workshop"])
can_write = RoleChecker(["admin"])
admin_only = RoleChecker(["admin"])


class MaterialPayload(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    paper_composition: str | None = None
    layer_count: int | None = Field(default=None, ge=1)
    flute_type: Literal["AB", "BE", "A", "B", "E"] = "AB"
    basis_weight_description: str | None = None
    quote_price: Decimal | None = Field(default=None, ge=0)
    price_unit: str | None = None
    supplier_name: str | None = None
    quote_date: date | None = None
    remarks: str | None = None
    is_active: bool = True


def normalize_basis_weight(value: str | None) -> str | None:
    if not value:
        return None
    values = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:g|克)",
        value,
        flags=re.IGNORECASE,
    )
    if not values:
        parts = [part.strip() for part in value.split("/")]
        if parts and all(
            re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts
        ):
            values = parts
    return "/".join(f"{number}g" for number in values) or None


class MaterialResponse(MaterialPayload):
    model_config = ConfigDict(from_attributes=True)

    id: int
    flute_type: str


WORKSHOP_FIELDS = (
    "id",
    "code",
    "paper_composition",
    "layer_count",
    "flute_type",
    "basis_weight_description",
    "is_active",
)


def _validate_layer_flute(layer_count: int | None, flute_type: str | None) -> None:
    """v0.19.2-B B-5: 校验层数 × 楞型合法性。

    复用 flute_mapping.validate_flute_consistency：
      合法 3+A/B/E、5+AB/BE；非法 3+AB/BE、5+A/B/E。
    七层暂无规则：拒绝并提示待维护（不允许乱填七层楞型组合）。
    """
    if layer_count == 7:
        raise HTTPException(status_code=400, detail="七层楞型规则待维护，暂不支持新增/编辑七层材质。")
    error = validate_flute_consistency(flute_type, layer_count)
    if error:
        raise HTTPException(status_code=400, detail=error)


def _material_or_404(db: Session, material_id: int) -> Material:
    material = db.get(Material, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="材质不存在")
    return material


def _response(material: Material, user: User) -> dict:
    data = MaterialResponse.model_validate(material).model_dump()
    if user.role == "workshop":
        return {key: data[key] for key in WORKSHOP_FIELDS}
    return data


@router.get("")
def list_materials(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
    layer_count: int | None = Query(default=None, ge=1),
    flute_type: str | None = Query(default=None),
    supplier_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    # v0.19.2-B: 支持按层数（三/五/七层）与楞型过滤
    # Hotfix-2: 新增 supplier_name 过滤（精确匹配）
    filters = []
    if layer_count is not None:
        filters.append(Material.layer_count == layer_count)
    if flute_type:
        filters.append(Material.flute_type == flute_type)
    if supplier_name:
        filters.append(Material.supplier_name == supplier_name)

    total = db.scalar(
        select(func.count(Material.id)).where(*filters)
    ) or 0
    items = db.scalars(
        select(Material)
        .where(*filters)
        .order_by(Material.code)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_response(item, user) for item in items],
    }


@router.get("/{material_id}")
def get_material(
    material_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return _response(_material_or_404(db, material_id), user)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_material(
    payload: MaterialPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    _validate_layer_flute(payload.layer_count, payload.flute_type)
    data = payload.model_dump()
    data["code"] = clean_code(payload.code)
    data["basis_weight_description"] = normalize_basis_weight(
        payload.basis_weight_description
    )
    material = Material(**data)
    try:
        db.add(material)
        db.flush()
        audit_master_change(
            db,
            user=user,
            action="CREATE",
            resource="Material",
            resource_id=material.id,
            details=payload.model_dump(),
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="材质编码重复") from error
    db.refresh(material)
    return _response(material, user)


@router.put("/{material_id}")
def update_material(
    material_id: int,
    payload: MaterialPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    material = _material_or_404(db, material_id)
    _validate_layer_flute(payload.layer_count, payload.flute_type)
    before = MaterialResponse.model_validate(material).model_dump()
    for key, value in payload.model_dump().items():
        setattr(material, key, value)
    material.code = clean_code(payload.code)
    material.basis_weight_description = normalize_basis_weight(
        payload.basis_weight_description
    )
    try:
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="Material",
            resource_id=material.id,
            details={"before": before, "after": payload.model_dump()},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="材质编码重复") from error
    db.refresh(material)
    return _response(material, user)


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(
    material_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> Response:
    material = _material_or_404(db, material_id)
    audit_master_change(
        db,
        user=user,
        action="DELETE",
        resource="Material",
        resource_id=material.id,
        details={"code": material.code},
    )
    try:
        db.delete(material)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="材质已被产品使用，不能删除") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
