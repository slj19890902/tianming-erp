"""
Phase 17: 楞型识别与批量补全服务。

规则（按优先级）：
1. 后缀覆盖规则：material_text 末尾含 /E、/B、/A（不区分大小写） → 覆盖楞型
   - /E → E型三层  layer_count=3
   - /B → B型三层  layer_count=3
   - /A → A型五层  layer_count=5
2. 克重段数规则（以"/"分割，跳过首尾含 W/白 的段）：
   - 5 个克重段 → AB 型五层  layer_count=5
   - 3 个克重段 → A 型三层   layer_count=3（若无后缀推断）
   注："W"/"白" 开头的段为面纸材质标识，不计入层数
3. 无法识别 → flute_type=None, layer_count=None
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.product import Product


# ---------------------------------------------------------------------------
# 解析核心
# ---------------------------------------------------------------------------

_SUFFIX_RE = re.compile(r"/([EBAeba])$")
_WEIGHT_RE = re.compile(r"^\d+")  # 是否以数字开头（克重段）


def _is_surface_marker(segment: str) -> bool:
    """判断一个克重段是否为面纸类型标识（W/白 开头），不计入层数。"""
    s = segment.strip()
    return s.upper().startswith("W") or s.startswith("白")


def _count_weight_segments(text: str) -> int:
    """
    统计 text 中的纸层段数（含 W/白 面纸标识段，它们仍算一层纸）。
    text 示例："120/160/160/160/120"  "W/160/160/160/120"
    注：W/白 段计入层数，但不能单独决定楞型。
    """
    parts = text.split("/")
    count = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 面纸标识段（W/白）或以数字开头的克重段，均计入纸层数
        if _is_surface_marker(part) or _WEIGHT_RE.match(part):
            count += 1
    return count


@dataclass
class FluteParseResult:
    flute_type: str | None = None
    layer_count: int | None = None
    surface_paper_type: str | None = None
    source: str = "unknown"  # "suffix" / "weight_count" / "material" / "unrecognized"


def parse_flute_from_text(text: str | None) -> FluteParseResult:
    """
    从 legacy_material_text（或品名描述）解析楞型。
    """
    if not text or not text.strip():
        return FluteParseResult(source="unrecognized")

    t = text.strip()

    # 1. 后缀覆盖规则
    m = _SUFFIX_RE.search(t)
    if m:
        suffix = m.group(1).upper()
        if suffix == "E":
            return FluteParseResult(flute_type="E", layer_count=3, source="suffix")
        if suffix == "B":
            return FluteParseResult(flute_type="B", layer_count=3, source="suffix")
        if suffix == "A":
            return FluteParseResult(flute_type="A", layer_count=5, source="suffix")

    # 2. 克重段数规则
    seg_count = _count_weight_segments(t)
    if seg_count == 5:
        return FluteParseResult(flute_type="AB", layer_count=5, source="weight_count")
    if seg_count == 3:
        return FluteParseResult(flute_type="A", layer_count=3, source="weight_count")

    return FluteParseResult(source="unrecognized")


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
# 批量预览 / 应用
# ---------------------------------------------------------------------------

@dataclass
class FlutePreviewRow:
    product_id: int
    product_code: str
    product_name: str
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

    result = parse_flute_from_text(product.legacy_material_text)
    return result


def preview_flute_mapping(db: Session) -> FluteMappingPreview:
    """
    只读预览：显示当前所有有效产品的楞型识别结果。
    """
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
            will_update += 1
            rows.append(
                FlutePreviewRow(
                    product_id=p.id,
                    product_code=p.product_code,
                    product_name=p.product_name,
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


def apply_flute_mapping(db: Session) -> FluteMappingResult:
    """
    写入批量楞型：只更新 flute_type 为 None 的有效产品。
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

        # 保存识别前原始文本快照（只写一次）
        if p.legacy_flute_text is None and p.legacy_material_text:
            p.legacy_flute_text = p.legacy_material_text

        p.flute_type = result.flute_type
        p.layer_count = result.layer_count
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
