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
from app.models.material_price_history import (
    MaterialPriceAdjustmentBatch,
    MaterialPriceHistory,
)
from app.models.user import User
from app.services import material_price_adjust as price_adjust
from app.services.flute_mapping import validate_flute_consistency
from app.services.pricing import PricingError, calculate_price


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


# v0.19.2-B 排序：供应商固定顺序 + 楞型顺序 + 逐层克重升序 + 平方报价 + 材质代码
SUPPLIER_ORDER = ("苏州嘉林亿", "昆山鸣朋", "苏州佳丰")
FLUTE_ORDER_3 = {"B": 0, "E": 1, "A": 2}
FLUTE_ORDER_5 = {"AB": 0, "BE": 1}


def _supplier_rank(name: str | None) -> int:
    """按固定供应商顺序排名；未匹配（其他供应商）排在最后。"""
    if not name:
        return len(SUPPLIER_ORDER) + 1
    for index, key in enumerate(SUPPLIER_ORDER):
        if key in name:
            return index
    return len(SUPPLIER_ORDER)


def _flute_rank(layer_count: int | None, flute_type: str | None) -> int:
    ft = flute_type or ""
    if layer_count == 5:
        return FLUTE_ORDER_5.get(ft, len(FLUTE_ORDER_5))
    # 三层及其他：按 B→E→A；组合楞（如 BE）取其中最靠前者
    ranks = [FLUTE_ORDER_3[ch] for ch in ("B", "E", "A") if ch in ft]
    return min(ranks) if ranks else len(FLUTE_ORDER_3)


def _parse_layer_weights(description: str | None) -> tuple[float, ...]:
    """从 basis_weight_description（如 '150g/100g/100g'）解析逐层克重。"""
    if not description:
        return ()
    numbers = re.findall(r"\d+(?:\.\d+)?", description)
    return tuple(float(n) for n in numbers)


def _material_search_haystack(m: Material) -> str:
    """构建材质搜索匹配串：代码 + 供应商 + 克重结构 + 报价。"""
    weights = "/".join(str(int(w)) if w == int(w) else str(w) for w in _parse_layer_weights(m.basis_weight_description))
    parts = [
        m.code or "",
        m.supplier_name or "",
        m.basis_weight_description or "",
        weights,
        "" if m.quote_price is None else str(m.quote_price),
        m.paper_composition or "",
    ]
    return " ".join(parts).lower().replace(" ", "")


def _sort_materials(materials: list[Material], sort: str) -> list[Material]:
    """统一排序逻辑，前后端一致。

    - common（常用优先）: 供应商顺序 → 楞型顺序 → 逐层克重升序 → 平方报价 → 材质代码
    - weight（克重从低到高）: 逐层克重升序 → 平方报价 → 供应商顺序 → 材质代码
    - price（价格从低到高）: 平方报价升序 → 供应商顺序 → 逐层克重 → 材质代码
    """
    big = float("inf")

    def price_of(m: Material) -> float:
        return float(m.quote_price) if m.quote_price is not None else big

    def weights_of(m: Material) -> tuple[float, ...]:
        return _parse_layer_weights(m.basis_weight_description) or (big,)

    if sort == "weight":
        key = lambda m: (weights_of(m), price_of(m), _supplier_rank(m.supplier_name), m.code or "")
    elif sort == "price":
        key = lambda m: (price_of(m), _supplier_rank(m.supplier_name), weights_of(m), m.code or "")
    else:  # common
        key = lambda m: (
            _supplier_rank(m.supplier_name),
            _flute_rank(m.layer_count, m.flute_type),
            weights_of(m),
            price_of(m),
            m.code or "",
        )
    return sorted(materials, key=key)


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
    sort: Literal["common", "weight", "price"] = Query(default="common"),
    keyword: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    # v0.19.2-B: 支持按层数（三/五/七层）与楞型过滤
    # Hotfix-2: 新增 supplier_name 过滤（精确匹配）
    # v0.19.2-B 排序: sort=common|weight|price，默认 common（常用优先）
    # v0.19.2 下一轮: keyword 关键字搜索（代码/供应商/克重结构/纸种说明/报价），
    #   语义上「先经过 supplier+layer+flute 过滤，再按关键字匹配」。
    filters = []
    if layer_count is not None:
        filters.append(Material.layer_count == layer_count)
    if flute_type:
        filters.append(Material.flute_type == flute_type)
    if supplier_name:
        filters.append(Material.supplier_name == supplier_name)

    # 逐层克重排序需解析文本字段，因此在 Python 层统一排序后再分页，保证稳定一致
    all_rows = list(db.scalars(select(Material).where(*filters)).all())
    if keyword:
        kw = keyword.strip().lower().replace(" ", "")
        all_rows = [m for m in all_rows if kw in _material_search_haystack(m)]
    total = len(all_rows)
    ordered = _sort_materials(all_rows, sort)
    items = ordered[(page - 1) * page_size : (page - 1) * page_size + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_response(item, user) for item in items],
    }


class PriceAdjustRequest(BaseModel):
    supplier_name: str = Field(min_length=1)
    adjust_percent: str = Field(min_length=1)
    effective_date: date | None = None
    remark: str | None = None


class BoardCostRequest(BaseModel):
    box_category: Literal["normal", "die_cut"] = "normal"
    board_square_price: Decimal
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    unfolded_length_mm: Decimal | None = None
    unfolded_width_mm: Decimal | None = None


@router.post("/price-adjustments/preview")
def preview_price_adjustment(
    payload: PriceAdjustRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    """供应商调价 dry-run 预览：只读，绝不写库。"""
    try:
        return price_adjust.preview(
            db,
            supplier_name=payload.supplier_name,
            adjust_percent_raw=payload.adjust_percent,
            effective_date=payload.effective_date,
        )
    except price_adjust.PriceAdjustError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/price-adjustments/apply")
def apply_price_adjustment(
    payload: PriceAdjustRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    """供应商调价应用：先备份数据库，再写 materials + 批次 + 历史。"""
    try:
        return price_adjust.apply(
            db,
            supplier_name=payload.supplier_name,
            adjust_percent_raw=payload.adjust_percent,
            effective_date=payload.effective_date,
            remark=payload.remark,
            operator=user.username,
        )
    except price_adjust.PriceAdjustError as error:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/price-adjustments")
def list_price_adjustment_batches(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    rows = list(
        db.scalars(
            select(MaterialPriceAdjustmentBatch).order_by(
                MaterialPriceAdjustmentBatch.created_at.desc()
            )
        ).all()
    )
    return {
        "items": [
            {
                "id": b.id,
                "supplier_name": b.supplier_name,
                "adjust_percent": float(b.adjust_percent) if b.adjust_percent is not None else None,
                "effective_date": b.effective_date.isoformat() if b.effective_date else None,
                "affected_count": b.affected_count,
                "remark": b.remark,
                "operator": b.operator,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in rows
        ]
    }


@router.post("/board-cost")
def board_cost_reference(
    payload: BoardCostRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """纸板成本参考 = 展开面积 × 材质平方报价（复用 pricing.calculate_price）。

    仅支持 普通箱(normal)/A1 与 模切箱(die_cut)；其它箱型返回 supported=False。
    成本只含纸板，不含人工/损耗/印刷/模切/利润/税费。
    """
    try:
        result = calculate_price(
            box_category=payload.box_category,
            board_square_price=payload.board_square_price,
            length_mm=payload.length_mm,
            width_mm=payload.width_mm,
            height_mm=payload.height_mm,
            unfolded_length_mm=payload.unfolded_length_mm,
            unfolded_width_mm=payload.unfolded_width_mm,
        )
    except PricingError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "supported": True,
        "box_category": payload.box_category,
        "area_m2": float(result.area_m2),
        "board_cost": float(result.unit_price),
    }


@router.get("/{material_id}")
def get_material(
    material_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return _response(_material_or_404(db, material_id), user)


@router.get("/{material_id}/price-history")
def get_material_price_history(
    material_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    material = _material_or_404(db, material_id)
    rows = list(
        db.scalars(
            select(MaterialPriceHistory)
            .where(MaterialPriceHistory.material_id == material_id)
            .order_by(MaterialPriceHistory.created_at.asc())
        ).all()
    )
    return {
        "material_id": material_id,
        "material_code": material.code,
        "supplier_name": material.supplier_name,
        "current_price": float(material.quote_price) if material.quote_price is not None else None,
        "items": [
            {
                "id": h.id,
                "old_price": float(h.old_price) if h.old_price is not None else None,
                "new_price": float(h.new_price) if h.new_price is not None else None,
                "adjust_percent": float(h.adjust_percent) if h.adjust_percent is not None else None,
                "effective_date": h.effective_date.isoformat() if h.effective_date else None,
                "adjust_reason": h.adjust_reason,
                "operator": h.operator,
                "batch_id": h.batch_id,
                "created_at": h.created_at.isoformat() if h.created_at else None,
            }
            for h in rows
        ],
    }


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
