from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
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
from app.models.supplier_paper_code import SupplierPaperCode
from app.models.user import User
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
from app.services import material_price_adjust as price_adjust
from app.services import material_pricing
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


class SupplierPaperCodePayload(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    code_char: str = Field(min_length=1, max_length=1)
    paper_name: str = Field(min_length=1, max_length=250)
    gram_weight: int = Field(gt=0, le=2000)
    paper_grade: str | None = Field(default=None, max_length=100)
    paper_role: str | None = Field(default=None, max_length=50)
    remark: str | None = None
    is_active: bool = True

    @field_validator("supplier_name", "paper_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("code_char")
    @classmethod
    def normalize_code_char(cls, value: str) -> str:
        code = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]", code):
            raise ValueError("基础代码必须是单个字母或数字")
        return code


class MaterialComposePreviewPayload(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    material_code: str = Field(min_length=1, max_length=5)
    flute_type: Literal["AB", "BE", "A", "B", "E"]

    @field_validator("supplier_name")
    @classmethod
    def strip_supplier_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("material_code")
    @classmethod
    def normalize_material_code(cls, value: str) -> str:
        code = value.strip().upper()
        if len(code) not in {3, 5}:
            raise ValueError("材质代码需为3位或5位")
        if not re.fullmatch(r"[A-Z0-9]+", code):
            raise ValueError("材质代码只能包含字母和数字")
        return code


class MaterialComposeSavePayload(MaterialComposePreviewPayload):
    quote_price: Decimal | None = Field(default=None, ge=0)
    remarks: str | None = None


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
    # 楞型加价：传 supplier_name + layer_count + flute_type 时，
    # 纸板成本用「基础平方报价 + 楞型加价」作为最终材料平方价。
    supplier_name: str | None = None
    layer_count: int | None = None
    flute_type: str | None = None


class FlutePriceRulePayload(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    layer_count: int = Field(ge=1)
    flute_type: str = Field(min_length=1, max_length=20)
    price_delta: Decimal = Decimal("0")
    effective_date: date | None = None
    remark: str | None = None
    is_active: bool = True


class FlutePriceRuleUpdate(BaseModel):
    price_delta: Decimal | None = None
    effective_date: date | None = None
    remark: str | None = None
    is_active: bool | None = None


class EffectivePriceRequest(BaseModel):
    material_id: int | None = None
    base_price: Decimal | None = None
    supplier_name: str | None = None
    layer_count: int | None = None
    flute_type: str | None = None


class CompareRequest(BaseModel):
    supplier_name: str | None = None
    layer_count: int | None = None
    flute_type: str | None = None
    material_id: int | None = None
    basis_weight_description: str | None = None


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
    若传 supplier_name+layer_count+flute_type，则用「基础价 + 楞型加价」为最终材料平方价。
    """
    base_price = payload.board_square_price
    delta_info = material_pricing.get_effective_material_price(
        db,
        base_price=base_price,
        supplier_name=payload.supplier_name,
        layer_count=payload.layer_count,
        flute_type=payload.flute_type,
    )
    effective_price = (
        Decimal(str(delta_info["effective_price"]))
        if delta_info["effective_price"] is not None
        else base_price
    )
    try:
        result = calculate_price(
            box_category=payload.box_category,
            board_square_price=effective_price,
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
        "base_price": delta_info["base_price"],
        "flute_delta": delta_info["flute_delta"],
        "effective_price": delta_info["effective_price"],
        "rule_id": delta_info["rule_id"],
    }


def _rule_dict(r: SupplierFlutePriceRule) -> dict:
    return {
        "id": r.id,
        "supplier_name": r.supplier_name,
        "layer_count": r.layer_count,
        "flute_type": r.flute_type,
        "price_delta": float(r.price_delta) if r.price_delta is not None else 0.0,
        "effective_date": r.effective_date.isoformat() if r.effective_date else None,
        "remark": r.remark,
        "is_active": bool(r.is_active),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/flute-price-rules")
def list_flute_price_rules(
    include_inactive: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    stmt = select(SupplierFlutePriceRule)
    if not include_inactive:
        stmt = stmt.where(SupplierFlutePriceRule.is_active.is_(True))
    stmt = stmt.order_by(
        SupplierFlutePriceRule.supplier_name,
        SupplierFlutePriceRule.layer_count,
        SupplierFlutePriceRule.flute_type,
    )
    return {"items": [_rule_dict(r) for r in db.scalars(stmt).all()]}


@router.post("/flute-price-rules", status_code=status.HTTP_201_CREATED)
def create_flute_price_rule(
    payload: FlutePriceRulePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    rule = SupplierFlutePriceRule(
        supplier_name=payload.supplier_name.strip(),
        layer_count=payload.layer_count,
        flute_type=payload.flute_type.strip().upper(),
        price_delta=payload.price_delta,
        effective_date=payload.effective_date,
        remark=payload.remark,
        is_active=payload.is_active,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_dict(rule)


@router.patch("/flute-price-rules/{rule_id}")
def update_flute_price_rule(
    rule_id: int,
    payload: FlutePriceRuleUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    rule = db.get(SupplierFlutePriceRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="楞型加价规则不存在")
    if payload.price_delta is not None:
        rule.price_delta = payload.price_delta
    if payload.effective_date is not None:
        rule.effective_date = payload.effective_date
    if payload.remark is not None:
        rule.remark = payload.remark
    if payload.is_active is not None:
        rule.is_active = payload.is_active
    db.commit()
    db.refresh(rule)
    return _rule_dict(rule)


@router.post("/flute-price-rules/{rule_id}/disable")
def disable_flute_price_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    rule = db.get(SupplierFlutePriceRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="楞型加价规则不存在")
    rule.is_active = False
    db.commit()
    db.refresh(rule)
    return _rule_dict(rule)


@router.post("/effective-price")
def effective_material_price(
    payload: EffectivePriceRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """统一最终材料平方价：基础价 + 楞型加价。常用箱/订单/比价/成本共用。"""
    material = None
    if payload.material_id is not None:
        material = db.get(Material, payload.material_id)
        if material is None:
            raise HTTPException(status_code=404, detail="材质不存在")
    return material_pricing.get_effective_material_price(
        db,
        material=material,
        base_price=payload.base_price,
        supplier_name=payload.supplier_name,
        layer_count=payload.layer_count,
        flute_type=payload.flute_type,
    )


@router.post("/compare")
def compare_materials_endpoint(
    payload: CompareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """同克重多供应商比价：含楞型加价后的最终可比价。

    candidate 选取：同层数（必填或取自 material）。如给 material_id/克重，限定到同克重组。
    """
    layer_count = payload.layer_count
    weight = payload.basis_weight_description
    if payload.material_id is not None:
        ref = db.get(Material, payload.material_id)
        if ref is not None:
            layer_count = layer_count or ref.layer_count
            weight = weight or ref.basis_weight_description

    stmt = select(Material).where(Material.is_active.is_(True))
    if layer_count is not None:
        stmt = stmt.where(Material.layer_count == layer_count)
    if weight:
        stmt = stmt.where(Material.basis_weight_description == weight)
    candidates = list(db.scalars(stmt).all())
    groups = material_pricing.compare_materials(
        db,
        candidates=candidates,
        layer_count=layer_count,
        flute_type=payload.flute_type,
    )
    return {
        "layer_count": layer_count,
        "flute_type": (payload.flute_type or "").strip().upper() or None,
        "groups": groups,
    }


def _paper_code_dict(row: SupplierPaperCode) -> dict:
    return {
        "id": row.id,
        "supplier_name": row.supplier_name,
        "code_char": row.code_char,
        "paper_name": row.paper_name,
        "gram_weight": row.gram_weight,
        "paper_grade": row.paper_grade,
        "paper_role": row.paper_role,
        "remark": row.remark,
        "is_active": row.is_active,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _compose_preview(
    db: Session,
    *,
    supplier_name: str,
    material_code: str,
    flute_type: str,
) -> dict:
    layer_count = len(material_code)
    _validate_layer_flute(layer_count, flute_type)
    roles = (
        ["面纸", "芯纸", "里纸"]
        if layer_count == 3
        else ["面纸", "芯纸", "中纸", "芯纸", "里纸"]
    )
    rows = list(
        db.scalars(
            select(SupplierPaperCode).where(
                SupplierPaperCode.supplier_name == supplier_name,
                SupplierPaperCode.code_char.in_(set(material_code)),
                SupplierPaperCode.is_active.is_(True),
            )
        ).all()
    )
    code_map = {row.code_char: row for row in rows}
    missing_codes = list(
        dict.fromkeys(char for char in material_code if char not in code_map)
    )
    layers = []
    total_weight = 0
    for index, (char, role) in enumerate(zip(material_code, roles), start=1):
        paper = code_map.get(char)
        if paper is not None:
            total_weight += paper.gram_weight
        layers.append(
            {
                "position": index,
                "role": role,
                "code_char": char,
                "paper_name": paper.paper_name if paper else None,
                "gram_weight": paper.gram_weight if paper else None,
                "paper_grade": paper.paper_grade if paper else None,
                "configured_role": paper.paper_role if paper else None,
                "missing": paper is None,
            }
        )
    existing = db.scalar(
        select(Material).where(
            func.upper(Material.code) == material_code,
            Material.supplier_name == supplier_name,
        )
    )
    valid = not missing_codes
    if missing_codes:
        message = f"该供应商下不存在基础代码：{'、'.join(missing_codes)}"
    elif existing is not None:
        message = "已匹配现有材质，平方价已带出"
    else:
        message = "当前组合未找到已有平方价，请手工填写平方价"
    return {
        "supplier_name": supplier_name,
        "material_code": material_code,
        "layer_count": layer_count,
        "flute_type": flute_type,
        "layers": layers,
        "total_gram_weight": total_weight if valid else None,
        "missing_codes": missing_codes,
        "valid": valid,
        "existing_material_id": existing.id if existing else None,
        "existing_square_price": existing.quote_price if existing else None,
        "message": message,
    }


@router.get("/paper-codes")
def list_supplier_paper_codes(
    supplier_name: str | None = None,
    keyword: str | None = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(SupplierPaperCode)
    if supplier_name:
        query = query.where(
            SupplierPaperCode.supplier_name == supplier_name.strip()
        )
    if not include_inactive:
        query = query.where(SupplierPaperCode.is_active.is_(True))
    if keyword:
        pattern = f"%{keyword.strip()}%"
        query = query.where(
            SupplierPaperCode.code_char.ilike(pattern)
            | SupplierPaperCode.paper_name.ilike(pattern)
            | SupplierPaperCode.paper_grade.ilike(pattern)
        )
    rows = list(
        db.scalars(
            query.order_by(
                SupplierPaperCode.supplier_name,
                SupplierPaperCode.code_char,
            )
        ).all()
    )
    return {"items": [_paper_code_dict(row) for row in rows], "total": len(rows)}


@router.post("/paper-codes", status_code=status.HTTP_201_CREATED)
def create_supplier_paper_code(
    payload: SupplierPaperCodePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    row = SupplierPaperCode(**payload.model_dump())
    try:
        db.add(row)
        db.flush()
        audit_master_change(
            db,
            user=user,
            action="CREATE",
            resource="SupplierPaperCode",
            resource_id=row.id,
            details=payload.model_dump(),
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="该供应商已存在相同基础代码",
        ) from error
    db.refresh(row)
    return _paper_code_dict(row)


@router.put("/paper-codes/{paper_code_id}")
def update_supplier_paper_code(
    paper_code_id: int,
    payload: SupplierPaperCodePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    row = db.get(SupplierPaperCode, paper_code_id)
    if row is None:
        raise HTTPException(status_code=404, detail="基础纸种代码不存在")
    before = _paper_code_dict(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    try:
        audit_master_change(
            db,
            user=user,
            action="UPDATE",
            resource="SupplierPaperCode",
            resource_id=row.id,
            details={"before": before, "after": payload.model_dump()},
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="该供应商已存在相同基础代码",
        ) from error
    db.refresh(row)
    return _paper_code_dict(row)


@router.post("/compose/preview")
def preview_material_composition(
    payload: MaterialComposePreviewPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(can_write),
) -> dict:
    return _compose_preview(
        db,
        supplier_name=payload.supplier_name,
        material_code=payload.material_code,
        flute_type=payload.flute_type,
    )


@router.post("/compose/save")
def save_material_composition(
    payload: MaterialComposeSavePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    preview = _compose_preview(
        db,
        supplier_name=payload.supplier_name,
        material_code=payload.material_code,
        flute_type=payload.flute_type,
    )
    if not preview["valid"]:
        raise HTTPException(status_code=400, detail=preview["message"])
    existing = (
        db.get(Material, preview["existing_material_id"])
        if preview["existing_material_id"] is not None
        else None
    )
    conflict = db.scalar(
        select(Material).where(func.upper(Material.code) == payload.material_code)
    )
    if existing is None and conflict is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"材质代码 {payload.material_code} 已被供应商"
                f"“{conflict.supplier_name or '未设置'}”使用；当前全局唯一约束下不能重复保存"
            ),
        )
    if existing is None and payload.quote_price is None:
        raise HTTPException(
            status_code=400,
            detail="当前组合没有已有平方价，请手工填写平方价",
        )
    weights = [str(layer["gram_weight"]) for layer in preview["layers"]]
    composition = " | ".join(
        (
            f'{layer["role"]}:{layer["code_char"]}='
            f'{layer["gram_weight"]}g {layer["paper_name"]}'
        )
        for layer in preview["layers"]
    )
    if existing is None:
        material = Material(
            code=payload.material_code,
            paper_composition=composition,
            layer_count=preview["layer_count"],
            flute_type=payload.flute_type,
            basis_weight_description="/".join(f"{weight}g" for weight in weights),
            quote_price=payload.quote_price,
            price_unit="元/㎡",
            supplier_name=payload.supplier_name,
            remarks=(payload.remarks or "").strip() or None,
            is_active=True,
        )
        db.add(material)
        action = "CREATE"
        before = None
    else:
        material = existing
        before = _response(material, user)
        material.paper_composition = composition
        material.layer_count = preview["layer_count"]
        material.flute_type = payload.flute_type
        material.basis_weight_description = "/".join(
            f"{weight}g" for weight in weights
        )
        if payload.quote_price is not None:
            material.quote_price = payload.quote_price
        material.price_unit = material.price_unit or "元/㎡"
        material.remarks = (payload.remarks or "").strip() or material.remarks
        material.is_active = True
        action = "UPDATE"
    try:
        db.flush()
        audit_master_change(
            db,
            user=user,
            action=action,
            resource="Material",
            resource_id=material.id,
            details={
                "before": before,
                "composer": payload.model_dump(),
                "total_gram_weight": preview["total_gram_weight"],
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="材质编码重复") from error
    db.refresh(material)
    return {
        "material": _response(material, user),
        "total_gram_weight": preview["total_gram_weight"],
        "created": action == "CREATE",
        "message": "组合材质已保存，可在常用箱、订单、报料和报价中选择",
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
