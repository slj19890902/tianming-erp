from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
import unicodedata
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, RoleChecker, get_db, has_permission
from app.api.master_data_common import audit_master_change, clean_code
from app.core.time_contract import utc_naive_to_api
from app.models.material import Material
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.material_price_history import (
    MaterialPriceAdjustmentBatch,
    MaterialPriceHistory,
)
from app.models.supplier_paper_code import SupplierPaperCode
from app.models.user import User
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule
from app.services import material_price_adjust as price_adjust
from app.services import material_pricing
from app.services.corrugated_material_pricing import estimate_material_price
from app.services.flute_mapping import seven_layer_code_error, validate_flute_consistency
from app.services.pricing import PricingError, calculate_price
from app.services.master_data_versioning import (
    apply_versioned_update,
    record_versioned_create,
)


router = APIRouter()
can_read = PermissionChecker("products.view")
can_cost = PermissionChecker("cost.view")
can_write = RoleChecker(["admin"])
admin_only = RoleChecker(["admin"])


class MaterialPayload(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    paper_composition: str | None = None
    layer_count: int | None = Field(default=None, ge=1)
    flute_type: Literal["AB", "BE", "A", "B", "E", "AAA", "ABC"] | None = None
    basis_weight_description: str | None = None
    quote_price: Decimal | None = Field(default=None, ge=0)
    rule_base_price: Decimal | None = Field(default=None, ge=0)
    price_source: str | None = None
    price_unit: str | None = None
    supplier_name: str | None = None
    quote_date: date | None = None
    remarks: str | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def validate_seven_layer_code(self) -> "MaterialPayload":
        error = seven_layer_code_error(self.code, self.layer_count)
        if error:
            raise ValueError(error)
        return self


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
    flute_type: str | None
    version: int


class MaterialMutationPayload(BaseModel):
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


class MaterialUpdatePayload(MaterialPayload):
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
        code = unicodedata.normalize("NFKC", value or "").strip()
        if len(code) != 1 or not code.isprintable() or code.isspace():
            raise ValueError("基础代码必须是单个可见字符")
        code = code.upper()
        if len(code) != 1:
            raise ValueError("基础代码必须是单个可见字符")
        return code


class MaterialComposePreviewPayload(BaseModel):
    supplier_name: str = Field(min_length=1, max_length=200)
    material_code: str = Field(min_length=1, max_length=7)
    layer_count: int | None = Field(default=None, ge=3, le=7)
    usage_flute_type: Literal["AB", "BE", "A", "B", "E", "AAA", "ABC"] | None = None
    quote_price: Decimal | None = Field(default=None, ge=0)

    @field_validator("supplier_name")
    @classmethod
    def strip_supplier_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("material_code")
    @classmethod
    def normalize_material_code(cls, value: str) -> str:
        code = unicodedata.normalize("NFKC", value or "").strip().upper()
        if len(code) not in {3, 5, 7}:
            raise ValueError("材质代码需为3位、5位或7位")
        if any(not char.isprintable() or char.isspace() for char in code):
            raise ValueError("材质代码只能包含可见字符，不能包含空格")
        return code

    @model_validator(mode="after")
    def require_seven_layer_flute(self) -> "MaterialComposePreviewPayload":
        if len(self.material_code) == 7 and self.usage_flute_type not in {"AAA", "ABC"}:
            raise ValueError("七层材质必须人工选择楞型（AAA 或 ABC）")
        return self


class MaterialComposeSavePayload(MaterialComposePreviewPayload):
    remarks: str | None = None
    parsed_supplier_name: str = Field(min_length=1, max_length=200)
    parsed_material_code: str = Field(min_length=1, max_length=7)
    parsed_layer_count: int = Field(ge=3, le=7)
    price_source: Literal["manual", "suggested"]


WORKSHOP_FIELDS = (
    "id",
    "code",
    "paper_composition",
    "layer_count",
    "flute_type",
    "basis_weight_description",
    "is_active",
    "version",
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
    """校验层数与楞型组合，且保留历史空楞型的读取兼容性。"""
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
    if not has_permission(user, "cost.view"):
        for field in ("quote_price", "rule_base_price", "price_source"):
            data.pop(field, None)
    if user.role == "workshop":
        return {key: data[key] for key in WORKSHOP_FIELDS}
    return data


def _material_write_data(payload: MaterialPayload) -> dict:
    data = payload.model_dump(include=set(MaterialPayload.model_fields))
    data["code"] = clean_code(payload.code)
    data["flute_type"] = None
    data["basis_weight_description"] = normalize_basis_weight(
        payload.basis_weight_description
    )
    return data


def _changed_updates(material: Material, updates: dict) -> dict:
    return {
        key: value
        for key, value in updates.items()
        if getattr(material, key) != value
    }


def _has_version_history(db: Session, material_id: int) -> bool:
    return db.scalar(
        select(MasterDataObjectVersion.id)
        .where(
            MasterDataObjectVersion.object_type == "material",
            MasterDataObjectVersion.object_id == material_id,
        )
        .limit(1)
    ) is not None


def _raise_material_version_conflict(
    material: Material,
    expected_version: int,
) -> None:
    if material.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MASTER_VERSION_CONFLICT",
                "expected_version": expected_version,
                "current_version": material.version,
            },
        )


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


class PriceAdjustPreviewRequest(BaseModel):
    supplier_name: str = Field(min_length=1)
    adjust_percent: str = Field(min_length=1)
    effective_date: date | None = None


class PriceAdjustApplyRequest(PriceAdjustPreviewRequest):
    expected_versions: dict[int, int]
    change_reason: str = Field(min_length=1)
    confirmation_tokens: dict[int, str] = Field(default_factory=dict)

    @field_validator("expected_versions")
    @classmethod
    def validate_expected_versions(cls, value: dict[int, int]) -> dict[int, int]:
        if any(version < 1 for version in value.values()):
            raise ValueError("预期版本必须大于等于1")
        return value

    @field_validator("change_reason")
    @classmethod
    def validate_change_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("修改原因不能为空")
        return reason


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
    payload: PriceAdjustPreviewRequest,
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
    payload: PriceAdjustApplyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    """供应商调价应用：先备份数据库，再写 materials + 批次 + 历史。"""
    try:
        result = price_adjust.apply(
            db,
            supplier_name=payload.supplier_name,
            adjust_percent_raw=payload.adjust_percent,
            effective_date=payload.effective_date,
            expected_versions=payload.expected_versions,
            change_reason=payload.change_reason,
            confirmation_tokens=payload.confirmation_tokens,
            user=user,
            operator=user.username,
        )
        db.commit()
        return result
    except price_adjust.PriceAdjustConflictError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except price_adjust.PriceAdjustError as error:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.get("/price-adjustments")
def list_price_adjustment_batches(
    db: Session = Depends(get_db),
    user: User = Depends(can_cost),
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
                "created_at": utc_naive_to_api(b.created_at),
            }
            for b in rows
        ]
    }


@router.post("/board-cost")
def board_cost_reference(
    payload: BoardCostRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_cost),
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
        "created_at": utc_naive_to_api(r.created_at),
        "updated_at": utc_naive_to_api(r.updated_at) if r.updated_at else None,
    }


@router.get("/flute-price-rules")
def list_flute_price_rules(
    include_inactive: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(can_cost),
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
    user: User = Depends(can_cost),
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
    user: User = Depends(can_cost),
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
    if payload.supplier_name:
        stmt = stmt.where(Material.supplier_name == payload.supplier_name)
    numeric_total = None
    if weight and re.fullmatch(r"\d+(?:\.\d+)?", str(weight).strip()):
        numeric_total = Decimal(str(weight).strip())
    elif weight:
        stmt = stmt.where(Material.basis_weight_description == weight)
    candidates = list(db.scalars(stmt).all())
    if numeric_total is not None:
        candidates = [
            material
            for material in candidates
            if sum(Decimal(str(value)) for value in _parse_layer_weights(
                material.basis_weight_description
            )) == numeric_total
        ]
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
        "created_at": utc_naive_to_api(row.created_at),
        "updated_at": utc_naive_to_api(row.updated_at) if row.updated_at else None,
    }


def _dictionary_material_code(value: str | None, layer_count: int) -> str:
    compact = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    compact = re.sub(r"\s+", "", compact)
    return compact[:layer_count]


def _find_dictionary_duplicate(
    db: Session,
    *,
    supplier_name: str,
    layer_count: int,
    material_code: str,
    exclude_material_id: int | None = None,
) -> Material | None:
    normalized = _dictionary_material_code(material_code, layer_count)
    rows = db.scalars(
        select(Material).where(
            Material.supplier_name == supplier_name,
            Material.layer_count == layer_count,
        )
    ).all()
    return next(
        (
            row for row in rows
            if row.id != exclude_material_id
            and _dictionary_material_code(row.code, layer_count) == normalized
        ),
        None,
    )


def _compose_preview(
    db: Session,
    *,
    supplier_name: str,
    material_code: str,
    usage_flute_type: str | None = None,
    manual_quote_price: Decimal | None = None,
    requested_layer_count: int | None = None,
) -> dict:
    layer_count = len(material_code)
    if requested_layer_count is not None and requested_layer_count != layer_count:
        raise HTTPException(
            status_code=400,
            detail=f"当前选择{requested_layer_count}层，但材质代码为{layer_count}位，请检查后重新解析",
        )
    roles = (
        ["面纸", "第一楞纸", "第一芯纸", "第二楞纸", "第二芯纸", "第三楞纸", "里纸"]
        if layer_count == 7
        else
        ["面纸", "瓦楞纸", "里纸"]
        if layer_count == 3
        else ["面纸", "B楞瓦纸", "芯纸", "A楞瓦纸", "里纸"]
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
    existing = _find_dictionary_duplicate(
        db,
        supplier_name=supplier_name,
        layer_count=layer_count,
        material_code=material_code,
    )
    valid = not missing_codes
    pricing = (
        estimate_material_price(
            db,
            supplier_name=supplier_name,
            material_code=material_code,
            usage_flute_type=usage_flute_type,
        )
        if valid and layer_count in {3, 5}
        else {"calculable": False}
    )
    if valid and layer_count == 7:
        pricing["message"] = "七层材质暂无自动报价公式，请使用人工价格"
    if missing_codes:
        message = f"该供应商下不存在基础代码：{'、'.join(missing_codes)}"
    elif existing is not None:
        layer_label = {3: "三层", 5: "五层"}.get(layer_count, f"{layer_count}层")
        message = (
            f"该供应商下已存在{layer_label}材质代码 {material_code}，"
            "不能重复保存。楞型请在常用箱中选择。"
        )
    elif manual_quote_price is not None:
        message = "已手工填写平方价，可保存为可用材质"
    elif pricing.get("calculable"):
        message = "已按供应商基准报价和当前调价规则推算，可保存为可用材质"
    else:
        message = "当前组合未找到已有平方价，请手工填写平方价"
    return {
        "supplier_name": supplier_name,
        "material_code": material_code,
        "layer_count": layer_count,
        "usage_flute_type": usage_flute_type,
        "layers": layers,
        "total_gram_weight": total_weight if valid else None,
        "missing_codes": missing_codes,
        "valid": valid,
        "existing_material_id": existing.id if existing else None,
        "duplicate_material": existing is not None,
        "existing_square_price": existing.quote_price if existing else None,
        "quotation_base_price": pricing.get("quotation_base_price"),
        "usage_base_price": pricing.get("usage_base_price"),
        "current_suggested_price": pricing.get("current_suggested_price"),
        "adjustment_percent": pricing.get("adjustment_percent"),
        "adjustment_effective_date": pricing.get("adjustment_effective_date"),
        "price_calculation": pricing,
        "message": message,
        "parse_key": f"{supplier_name}|{layer_count}|{material_code}",
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
        usage_flute_type=payload.usage_flute_type,
        manual_quote_price=payload.quote_price,
        requested_layer_count=payload.layer_count,
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
        usage_flute_type=payload.usage_flute_type,
        manual_quote_price=payload.quote_price,
        requested_layer_count=payload.layer_count,
    )
    if (
        payload.parsed_supplier_name.strip() != payload.supplier_name
        or payload.parsed_material_code.strip().upper() != payload.material_code
        or payload.parsed_layer_count != preview["layer_count"]
    ):
        raise HTTPException(
            status_code=409,
            detail="当前输入已发生变化，旧解析结果已失效，请重新解析后保存",
        )
    if not preview["valid"]:
        raise HTTPException(status_code=400, detail=preview["message"])
    existing = _find_dictionary_duplicate(
        db,
        supplier_name=payload.supplier_name,
        layer_count=preview["layer_count"],
        material_code=payload.material_code,
    )
    if existing is not None:
        layer_label = {3: "三层", 5: "五层"}.get(
            payload.layer_count, f"{payload.layer_count}层"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"该供应商下已存在{layer_label}材质代码 "
                f"{payload.material_code}，不能重复保存。楞型请在常用箱中选择。"
            ),
        )
    conflict = db.scalar(
        select(Material).where(func.upper(Material.code) == payload.material_code)
    )
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"材质代码 {payload.material_code} 已被供应商"
                f"“{conflict.supplier_name or '未设置'}”使用；当前全局唯一约束下不能重复保存"
            ),
        )
    suggested_price = preview.get("current_suggested_price")
    if payload.quote_price is None and suggested_price is None:
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
    if payload.price_source == "suggested":
        if suggested_price is None:
            raise HTTPException(status_code=409, detail="当前材质没有可用建议价，请重新解析或手工填写平方价")
        final_price = suggested_price
    else:
        if payload.quote_price is None:
            raise HTTPException(status_code=400, detail="请填写当前材质的手工平方价")
        final_price = payload.quote_price
    material = Material(
            code=payload.material_code,
            paper_composition=composition,
            layer_count=preview["layer_count"],
            flute_type=None,
            basis_weight_description="/".join(f"{weight}g" for weight in weights),
            quote_price=final_price,
            rule_base_price=preview.get("quotation_base_price"),
            price_source=(
                f"供应商规则基准价 {preview.get('quotation_base_price')}；"
                f"当前调价 {preview.get('adjustment_percent') or 0}%"
                if preview.get("quotation_base_price") is not None
                else "人工填写"
            ),
            price_unit="元/㎡",
            supplier_name=payload.supplier_name,
            remarks=(payload.remarks or "").strip() or None,
            is_active=True,
        )
    db.add(material)
    action = "CREATE"
    before = None
    try:
        db.flush()
        record_versioned_create(
            db,
            object_type="material",
            entity=material,
            user=user,
            reason="新增材质",
            source="api.materials.compose.save",
        )
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
    except Exception:
        db.rollback()
        raise
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
    user: User = Depends(can_cost),
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
                "created_at": utc_naive_to_api(h.created_at),
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
    data = _material_write_data(payload)
    duplicate = _find_dictionary_duplicate(
        db,
        supplier_name=(payload.supplier_name or "").strip(),
        layer_count=payload.layer_count or len(data["code"]),
        material_code=data["code"],
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该供应商下已存在材质代码 {data['code']}，不能重复保存",
        )
    material = Material(**data)
    try:
        db.add(material)
        db.flush()
        record_versioned_create(
            db,
            object_type="material",
            entity=material,
            user=user,
            reason="新增材质",
            source="api.materials.create",
        )
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
    except Exception:
        db.rollback()
        raise
    db.refresh(material)
    return _response(material, user)


@router.put("/{material_id}")
def update_material(
    material_id: int,
    payload: MaterialUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_write),
) -> dict:
    material = _material_or_404(db, material_id)
    before = MaterialResponse.model_validate(material).model_dump()
    duplicate = _find_dictionary_duplicate(
        db,
        supplier_name=(payload.supplier_name or "").strip(),
        layer_count=payload.layer_count or len(payload.code),
        material_code=payload.code,
        exclude_material_id=material_id,
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该供应商下已存在材质代码 {payload.code}，不能重复保存",
        )
    updates = _material_write_data(payload)
    changed = _changed_updates(material, updates)
    try:
        apply_versioned_update(
            db,
            object_type="material",
            entity=material,
            updates=updates,
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
            source="api.materials.update",
            confirmation_token=payload.confirmation_token,
        )
        if changed:
            audit_master_change(
                db,
                user=user,
                action="UPDATE",
                resource="Material",
                resource_id=material.id,
                details={"before": before, "after": updates},
            )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="材质编码重复") from error
    except Exception:
        db.rollback()
        raise
    db.refresh(material)
    return _response(material, user)


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(
    material_id: int,
    payload: MaterialMutationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> Response:
    material = _material_or_404(db, material_id)
    try:
        apply_versioned_update(
            db,
            object_type="material",
            entity=material,
            updates={"is_active": False},
            expected_version=payload.expected_version,
            user=user,
            reason=payload.change_reason,
            source="api.materials.deactivate",
            action="soft_delete",
            confirmation_token=payload.confirmation_token,
        )
        audit_master_change(
            db,
            user=user,
            action="DISABLE",
            resource="Material",
            resource_id=material.id,
            details={"code": material.code, "reason": payload.change_reason},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)
