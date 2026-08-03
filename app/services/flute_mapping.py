"""
Phase 17 / v0.17.1: 楞型识别与批量补全服务。

=== 业务常识规则 ===
单楞（单层瓦楞）= 3层纸板：只能是 A / B / E
双楞（双层瓦楞）= 5层纸板：只能是 AB / BE

解析规则（按优先级）：
0. 歧义检测：若文本末尾出现多个单楞候选（如 B/E、A4B-B/E），标记为 ambiguous，
   不自动写楞型，留人工确认。
1. 后缀覆盖规则：material_text 末尾仅有单楞字母后缀（/E、/B、/A，不区分大小写）
   - /E → E型，layer_count=3（E 是单楞）
   - /B → B型，layer_count=3（B 是单楞）
   - /A → A型，layer_count=3（A 是单楞）
2. 克重段数规则（以"/"分割，含 W/白 面纸标识段仍计入层数）：
   - 5 个段 → AB 型五层  layer_count=5
   - 3 个段 → A 型三层   layer_count=3（默认；若文本没有明确楞型后缀）
3. 无法识别 → flute_type=None, layer_count=None, source="unrecognized"
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.product import Product
from app.models.user import User


def _confirmation_token(
    tokens: Mapping[str, str] | None,
    product_id: int,
) -> str | None:
    if tokens is None:
        return None
    return tokens.get(f"product:{product_id}")


def _apply_batch_versioned_update(
    db: Session,
    *,
    confirmation_token: str | None,
    preview_confirmed: bool,
    **kwargs: Any,
) -> Any:
    """Apply through the version service after an explicit batch preview gate."""

    from fastapi import HTTPException
    from app.services.master_data_versioning import apply_versioned_update

    try:
        return apply_versioned_update(
            db,
            confirmation_token=confirmation_token,
            **kwargs,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if (
            not preview_confirmed
            or exc.status_code != 409
            or detail.get("code") != "MASTER_CHANGE_CONFIRMATION_REQUIRED"
        ):
            raise
        return apply_versioned_update(
            db,
            confirmation_token=detail["confirmation_token"],
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

VALID_FLUTE_FOR_3LAYER: frozenset[str] = frozenset(["A", "B", "E"])
VALID_FLUTE_FOR_5LAYER: frozenset[str] = frozenset(["AB", "BE"])
VALID_FLUTE_FOR_7LAYER: frozenset[str] = frozenset(["AAA", "ABC"])
ALL_VALID_FLUTE: frozenset[str] = (
    VALID_FLUTE_FOR_3LAYER
    | VALID_FLUTE_FOR_5LAYER
    | VALID_FLUTE_FOR_7LAYER
)


# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

# 末尾仅一个单楞字母：/A  /B  /E（不区分大小写）
_SUFFIX_RE = re.compile(r"/([EBAeba])$")

# 是否以数字开头（克重段判断）
_WEIGHT_RE = re.compile(r"^\d+")

# 歧义检测：两个单楞候选字母相邻，仅被 / 或 - 分隔，且前者不是数字的一部分
# 匹配：B/E  A/B  A4B-B/E（其中 -B/E 会命中），但不匹配 80A/B（A前有数字）
_AMBIGUOUS_RE = re.compile(r"(?<!\d)([EBAeba])[-/]([EBAeba])(?!\d)")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _is_surface_marker(segment: str) -> bool:
    """W/白 开头的段 = 白面纸标识（计入层数，但不决定楞型）。"""
    s = segment.strip()
    return s.upper().startswith("W") or s.startswith("白")


def _detect_surface_paper(text: str) -> str | None:
    """如果任意段含 W 或 白，返回 'white'，否则 None。"""
    for part in text.split("/"):
        p = part.strip()
        if p.upper().startswith("W") or "白" in p:
            return "white"
    return None


def _count_weight_segments(text: str) -> int:
    """
    统计纸层段数（含 W/白 面纸段；跳过纯楞型字母段如 /A /B /E）。
    """
    parts = text.split("/")
    count = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 纯单楞字母后缀（A/B/E）不计入纸层数
        if part.upper() in ("A", "B", "E", "AB", "BE"):
            continue
        # 面纸标识（W/白）或以数字开头的克重段，均计入
        if _is_surface_marker(part) or _WEIGHT_RE.match(part):
            count += 1
    return count


# ---------------------------------------------------------------------------
# 一致性校验
# ---------------------------------------------------------------------------

def normalize_flute_type(flute_type: str | None) -> str | None:
    normalized = str(flute_type or "").strip().upper()
    return normalized or None


def seven_layer_code_error(
    material_code: str | None,
    layer_count: int | None,
) -> str | None:
    """Reject a seven-character board code disguised as another layer count.

    Legacy material descriptions may contain separators and prose, so this
    guard deliberately applies only to a clean seven-character dictionary
    code. Seven-layer entries themselves must use exactly seven visible
    paper-layer characters; supplier symbols such as ``+`` are valid.
    """
    code = unicodedata.normalize("NFKC", str(material_code or "")).strip().upper()
    code = re.sub(r"\s+", "", code)
    is_seven_code = (
        len(code) == 7
        and all(char.isprintable() and not char.isspace() for char in code)
    )
    if is_seven_code and layer_count != 7:
        return "7位材质代码必须按七层保存，不能声明为三层、五层或空层数"
    if layer_count == 7 and not is_seven_code:
        return "七层材质代码必须是7个可见字符"
    return None


def validate_flute_consistency(flute_type: str | None, layer_count: int | None) -> str | None:
    """
    校验楞型与层数的一致性。
    返回 None 表示合法；返回错误说明字符串表示非法组合。
    """
    flute_type = normalize_flute_type(flute_type)
    if not flute_type or not layer_count:
        return None
    if layer_count == 3 and flute_type not in VALID_FLUTE_FOR_3LAYER:
        return (
            f"三层瓦楞只能是 A / B / E，当前楞型 {flute_type!r} 不合法。"
            "（AB/BE 是五层双楞）"
        )
    if layer_count == 5 and flute_type not in VALID_FLUTE_FOR_5LAYER:
        return (
            f"五层瓦楞只能是 AB / BE，当前楞型 {flute_type!r} 不合法。"
            "（A/B/E 是三层单楞）"
        )
    if layer_count == 7 and flute_type not in VALID_FLUTE_FOR_7LAYER:
        return (
            f"七层瓦楞只能是 AAA / ABC，当前楞型 {flute_type!r} 不合法。"
            "（七层楞型需由业务页面人工选择）"
        )
    return None


def validate_flute_for_write(
    flute_type: str | None,
    layer_count: int | None,
) -> str | None:
    """Strict write-time validation while keeping legacy reads permissive.

    ``validate_flute_consistency`` intentionally accepts missing historical
    values. New writes additionally require an explicit AAA/ABC choice for
    seven-layer board and never infer a flute when the layer count is unknown.
    """
    normalized_flute = normalize_flute_type(flute_type)
    if layer_count == 7 and normalized_flute is None:
        return "七层瓦楞必须明确选择 AAA 或 ABC，楞型不能为空"
    if layer_count not in {3, 5, 7} and normalized_flute is not None:
        return "层数为空或未知时不能写入楞型，请先明确材质层数"
    return validate_flute_consistency(normalized_flute, layer_count)


# ---------------------------------------------------------------------------
# 解析核心
# ---------------------------------------------------------------------------

@dataclass
class FluteParseResult:
    flute_type: str | None = None
    layer_count: int | None = None
    surface_paper_type: str | None = None
    source: str = "unknown"  # "suffix"/"weight_count"/"material"/"ambiguous"/"unrecognized"


def parse_flute_from_text(text: str | None) -> FluteParseResult:
    """
    从 legacy_material_text（材质文本）解析楞型。
    规则见模块文档。
    """
    if not text or not text.strip():
        return FluteParseResult(source="unrecognized")

    t = text.strip()
    surface = _detect_surface_paper(t)

    # 0. 歧义检测：多个单楞候选 → 不自动识别
    if _AMBIGUOUS_RE.search(t):
        return FluteParseResult(
            surface_paper_type=surface,
            source="ambiguous",
        )

    # 1. 单楞后缀规则（/A /B /E 末尾）
    m = _SUFFIX_RE.search(t)
    if m:
        suffix = m.group(1).upper()
        # 单楞字母 → 三层单楞
        layer = 3
        return FluteParseResult(
            flute_type=suffix,
            layer_count=layer,
            surface_paper_type=surface,
            source="suffix",
        )

    # 2. 克重段数规则
    seg_count = _count_weight_segments(t)
    if seg_count == 5:
        return FluteParseResult(
            flute_type="AB",
            layer_count=5,
            surface_paper_type=surface,
            source="weight_count",
        )
    if seg_count == 3:
        return FluteParseResult(
            flute_type="A",
            layer_count=3,
            surface_paper_type=surface,
            source="weight_count",
        )
    if seg_count == 7:
        # 七层没有默认楞型；仅识别层数，保留给业务页面人工选择 AAA/ABC。
        return FluteParseResult(
            layer_count=7,
            surface_paper_type=surface,
            source="weight_count",
        )

    return FluteParseResult(surface_paper_type=surface, source="unrecognized")


def parse_flute_from_material(material: Material | None) -> FluteParseResult:
    """从已关联的 Material 直接读取 flute_type / layer_count。"""
    if material is None:
        return FluteParseResult(source="unrecognized")
    return FluteParseResult(
        flute_type=material.flute_type or None,
        layer_count=material.layer_count or None,
        source="material",
    )


# ---------------------------------------------------------------------------
# 一致性修复（针对历史错误数据）
# ---------------------------------------------------------------------------

@dataclass
class FluteConsistencyFixResult:
    fixed_5layer_to_3: int = 0      # layer_count 5→3（单楞被误标为五层）
    fixed_3layer_to_null: int = 0   # flute_type 置 null（三层被误标为双楞）
    changes: list[dict] = field(default_factory=list)


def preview_flute_consistency_fix(db: Session) -> FluteConsistencyFixResult:
    """Return the exact consistency-repair plan without writing."""

    result = FluteConsistencyFixResult()
    bad_5 = db.query(Product).filter(
        Product.deleted_at.is_(None),
        Product.layer_count == 5,
        Product.flute_type.in_(list(VALID_FLUTE_FOR_3LAYER)),
    ).all()
    for product in bad_5:
        result.changes.append({
            "key": f"product:{product.id}",
            "object_type": "product",
            "object_id": product.id,
            "expected_version": int(product.version),
            "updates": {"layer_count": 3},
            "product_code": product.product_code,
            "fix": "5layer+singleflute->3layer",
            "flute_type": product.flute_type,
            "old_layer": 5,
            "new_layer": 3,
        })
        result.fixed_5layer_to_3 += 1

    bad_3 = db.query(Product).filter(
        Product.deleted_at.is_(None),
        Product.layer_count == 3,
        Product.flute_type.in_(list(VALID_FLUTE_FOR_5LAYER)),
    ).all()
    for product in bad_3:
        result.changes.append({
            "key": f"product:{product.id}",
            "object_type": "product",
            "object_id": product.id,
            "expected_version": int(product.version),
            "updates": {"flute_type": None},
            "product_code": product.product_code,
            "fix": "3layer+doubleflute->null",
            "old_flute_type": product.flute_type,
            "layer_count": 3,
        })
        result.fixed_3layer_to_null += 1
    return result


def apply_flute_consistency_fix(
    db: Session,
    *,
    user: User,
    confirmation_tokens: Mapping[str, str] | None = None,
    preview_confirmed: bool = False,
) -> FluteConsistencyFixResult:
    """
    修复非法楞型/层数组合，不覆盖 flute_type=None（只修复明确错误的记录）。
    调用方负责 db.commit() 和备份。

    情况 A：layer_count=5 且 flute_type 是单楞（A/B/E）
        → 单楞必然是 3 层，将 layer_count 改为 3。

    情况 B：layer_count=3 且 flute_type 是双楞（AB/BE）
        → 无法确定真实楞型，将 flute_type 置 null，保留 layer_count=3。
    """
    result = FluteConsistencyFixResult()

    # 情况 A: 5层 + 单楞 → 改 layer_count=3
    bad_5 = db.query(Product).filter(
        Product.deleted_at.is_(None),
        Product.layer_count == 5,
        Product.flute_type.in_(list(VALID_FLUTE_FOR_3LAYER)),
    ).all()

    for p in bad_5:
        result.changes.append({
            "product_id": p.id,
            "product_code": p.product_code,
            "fix": "5→3",
            "flute_type": p.flute_type,
            "old_layer": 5,
            "new_layer": 3,
        })
        _apply_batch_versioned_update(
            db,
            object_type="product",
            entity=p,
            updates={"layer_count": 3},
            expected_version=int(p.version),
            user=user,
            reason="系统楞型一致性修复批次",
            source="system.flute-mapping.fix-consistency",
            action="update",
            confirmation_token=_confirmation_token(confirmation_tokens, p.id),
            preview_confirmed=preview_confirmed,
        )
        result.fixed_5layer_to_3 += 1

    # 情况 B: 3层 + 双楞 → flute_type=null
    bad_3 = db.query(Product).filter(
        Product.deleted_at.is_(None),
        Product.layer_count == 3,
        Product.flute_type.in_(list(VALID_FLUTE_FOR_5LAYER)),
    ).all()

    for p in bad_3:
        result.changes.append({
            "product_id": p.id,
            "product_code": p.product_code,
            "fix": "3layer+doubleflute→null",
            "old_flute_type": p.flute_type,
            "layer_count": 3,
        })
        _apply_batch_versioned_update(
            db,
            object_type="product",
            entity=p,
            updates={"flute_type": None},
            expected_version=int(p.version),
            user=user,
            reason="系统楞型一致性修复批次",
            source="system.flute-mapping.fix-consistency",
            action="update",
            confirmation_token=_confirmation_token(confirmation_tokens, p.id),
            preview_confirmed=preview_confirmed,
        )
        result.fixed_3layer_to_null += 1

    return result


# ---------------------------------------------------------------------------
# 批量预览 / 应用
# ---------------------------------------------------------------------------

@dataclass
class FlutePreviewRow:
    product_id: int
    product_code: str
    product_name: str
    expected_version: int
    updates: dict[str, object]
    current_flute_type: str | None
    proposed_flute_type: str | None
    proposed_layer_count: int | None
    source: str
    legacy_flute_text: str | None


@dataclass
class FluteMappingPreview:
    total_products: int
    will_update: int
    already_set: int
    unrecognized: int
    rows: list[FlutePreviewRow] = field(default_factory=list)


@dataclass
class FluteMappingResult:
    updated: int
    skipped_already_set: int
    skipped_unrecognized: int
    changes: list[dict] = field(default_factory=list)


def _resolve_product_flute(product: Product) -> FluteParseResult:
    """
    决定一个产品的楞型：
    - 优先从关联 Material 读取（最可靠）
    - 次选从 legacy_material_text 解析
    """
    if product.material is not None:
        result = parse_flute_from_material(product.material)
        if result.flute_type:
            return result
        # The material dictionary intentionally does not store a default flute
        # for seven-layer board. Do not fall back to stale legacy text and
        # silently downgrade the product to a three/five-layer flute.
        if result.layer_count == 7:
            return result

    result = parse_flute_from_text(product.legacy_material_text)
    return result


def preview_flute_mapping(db: Session) -> FluteMappingPreview:
    """只读预览：显示当前所有有效产品的楞型识别结果（不写库）。"""
    products = (
        db.query(Product)
        .filter(Product.deleted_at.is_(None), Product.is_active.is_(True))
        .all()
    )

    total = len(products)
    will_update = 0
    already_set = 0
    unrecognized = 0
    rows: list[FlutePreviewRow] = []

    for p in products:
        if p.flute_type:
            already_set += 1
            continue

        result = _resolve_product_flute(p)
        if result.flute_type:
            updates: dict[str, object] = {
                "flute_type": result.flute_type,
                "layer_count": result.layer_count,
            }
            if result.surface_paper_type:
                updates["surface_paper_type"] = result.surface_paper_type
            will_update += 1
            rows.append(
                FlutePreviewRow(
                    product_id=p.id,
                    product_code=p.product_code,
                    product_name=p.product_name,
                    expected_version=int(p.version),
                    updates=updates,
                    current_flute_type=p.flute_type,
                    proposed_flute_type=result.flute_type,
                    proposed_layer_count=result.layer_count,
                    source=result.source,
                    legacy_flute_text=p.legacy_material_text,
                )
            )
        else:
            unrecognized += 1

    return FluteMappingPreview(
        total_products=total,
        will_update=will_update,
        already_set=already_set,
        unrecognized=unrecognized,
        rows=rows,
    )


def apply_flute_mapping(
    db: Session,
    *,
    user: User,
    confirmation_tokens: Mapping[str, str] | None = None,
    preview_confirmed: bool = False,
) -> FluteMappingResult:
    """
    批量写入楞型：只更新 flute_type 为 None 的有效产品。
    调用方负责 db.commit() 和备份。
    """
    products = (
        db.query(Product)
        .filter(Product.deleted_at.is_(None), Product.is_active.is_(True))
        .all()
    )

    updated = 0
    skipped_already_set = 0
    skipped_unrecognized = 0
    changes: list[dict] = []

    for p in products:
        if p.flute_type:
            skipped_already_set += 1
            continue

        result = _resolve_product_flute(p)
        if not result.flute_type:
            skipped_unrecognized += 1
            continue

        updates: dict[str, object] = {
            "flute_type": result.flute_type,
            "layer_count": result.layer_count,
        }
        if result.surface_paper_type:
            updates["surface_paper_type"] = result.surface_paper_type
        _apply_batch_versioned_update(
            db,
            object_type="product",
            entity=p,
            updates=updates,
            expected_version=int(p.version),
            user=user,
            reason="系统楞型映射批次更新常用箱",
            source="system.flute-mapping.apply",
            action="update",
            confirmation_token=_confirmation_token(confirmation_tokens, p.id),
            preview_confirmed=preview_confirmed,
        )
        updated += 1
        changes.append(
            {
                "product_id": p.id,
                "product_code": p.product_code,
                "product_name": p.product_name,
                "flute_type": result.flute_type,
                "layer_count": result.layer_count,
                "source": result.source,
            }
        )

    return FluteMappingResult(
        updated=updated,
        skipped_already_set=skipped_already_set,
        skipped_unrecognized=skipped_unrecognized,
        changes=changes,
    )
