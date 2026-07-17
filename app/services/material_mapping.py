"""material_mapping.py — 材质代码映射 & 客户料号批量规范服务。

所有写库操作必须在事务中完成。不允许跨模块直接写 products/materials，
统一通过本服务提供的函数调用。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models.user import User


def _confirmation_token(
    tokens: Mapping[str, str] | None,
    object_type: str,
    entity_id: int,
) -> str | None:
    if tokens is None:
        return None
    return tokens.get(f"{object_type}:{entity_id}")


def _version_actor(user: "User | None") -> Any:
    """Return the authenticated user or an explicit offline system actor."""

    return user or SimpleNamespace(id=None, username="system", role="admin")


def _apply_batch_versioned_update(
    db: "Session",
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


# ──────────────────────────────────────────────
# 1. 可信度分级
# ──────────────────────────────────────────────

def classify_confidence(
    confidence_label: str,
    old_code: str,
    new_code: str,
    is_conflict: bool,
) -> str:
    """将 CSV 的可信度/标注字段转为 高可信 / 中可信 / 低可信。"""
    c = (confidence_label or "").strip()

    if not old_code or not new_code:
        return "低可信"
    if is_conflict:
        return "低可信"
    if "无法解码" in c or "需人工" in c:
        return "低可信"
    if "7层板" in c:
        normalized_new_code = str(new_code or "").strip().upper()
        if (
            len(normalized_new_code) != 7
            or re.fullmatch(r"[A-Z0-9]{7}", normalized_new_code) is None
        ):
            return "低可信"

    # 差值抽取
    diff_match = re.search(r"差(\d+)g", c)
    diff_g = int(diff_match.group(1)) if diff_match else 0
    if diff_g >= 30:
        return "低可信"

    # 高可信：明确可替换 / 差0g / 差5g（非克重推定）
    if "可直接替换" in c:
        return "高可信"
    if "近似(差0g)" in c:
        return "高可信"
    if "近似(差5g)" in c and "纸种克重为推定" not in c:
        return "高可信"

    # 中可信：差10-25g / 克重推算 / 纸种推定
    if diff_g <= 25 or "克重推算" in c or "纸种克重为推定" in c:
        return "中可信"

    return "低可信"


# ──────────────────────────────────────────────
# 2. CSV 导入候选表
# ──────────────────────────────────────────────

@dataclass
class ImportStats:
    total: int = 0
    high: int = 0
    mid: int = 0
    low: int = 0
    conflicts: int = 0
    empty_old: int = 0
    empty_new: int = 0


def import_csv_to_candidates(
    db: "Session",
    csv_path: Path,
    source_file_name: str = "",
) -> ImportStats:
    """读取 CSV，写入 material_code_mapping_candidates，返回统计。

    规则：
    - 每行都导入（不跳过任何行）
    - 按 classify_confidence 分级
    - 默认 review_status = pending
    - 不覆盖已有记录（先清空同 source_file 的旧记录，再批量插入）
    """
    from app.models.material_mapping import MaterialCodeMappingCandidate

    stats = ImportStats()

    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # 检测字段名
    def _col(sample_row: dict, patterns: list[str]) -> str:
        for k in sample_row:
            for p in patterns:
                if p.strip() in k.strip():
                    return k
        return ""

    if not rows:
        return stats

    sample = rows[0]
    col_old    = _col(sample, ["ERP原代码", "原代码"])
    col_new    = _col(sample, ["嘉林亿新代码", "新代码"])
    col_price  = _col(sample, ["嘉林亿价", "新价"])
    col_supp   = _col(sample, ["识别供应商", "供应商"])
    col_layers = _col(sample, ["层数"])
    col_weight = _col(sample, ["逐层克重", "克重"])
    col_oldpri = _col(sample, ["原供应商价", "原价"])
    col_conf   = _col(sample, ["可信度", "标注"])

    # 先统计冲突（同旧代码对多个不同新代码）
    from collections import defaultdict
    code_map: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        old = (r.get(col_old) or "").strip()
        new = (r.get(col_new) or "").strip()
        if old:
            code_map[old].append(new)
    conflict_codes = {k for k, v in code_map.items() if len(set(v)) > 1}

    # 删除同 source_file 的旧记录，保证幂等
    if source_file_name:
        db.query(MaterialCodeMappingCandidate).filter(
            MaterialCodeMappingCandidate.source_file == source_file_name
        ).delete(synchronize_session=False)

    objects = []
    for row_num, r in enumerate(rows, start=2):  # 行2起（行1是标题）
        old  = (r.get(col_old) or "").strip()
        new  = (r.get(col_new) or "").strip()
        conf = (r.get(col_conf) or "").strip()
        supp = (r.get(col_supp) or "").strip()

        if not old:
            stats.empty_old += 1
        if not new:
            stats.empty_new += 1

        is_conflict = old in conflict_codes
        if is_conflict:
            stats.conflicts += 1

        level = classify_confidence(conf, old, new, is_conflict)

        def _to_decimal(s: str) -> Decimal | None:
            try:
                return Decimal(s.strip()) if s and s.strip() else None
            except InvalidOperation:
                return None

        obj = MaterialCodeMappingCandidate(
            old_code=old or "（空）",
            new_code=new or None,
            new_supplier=supp or None,
            old_supplier=supp or None,  # CSV 同字段，来源供应商
            layer_count=(r.get(col_layers) or "").strip() or None,
            weight_structure=(r.get(col_weight) or "").strip() or None,
            new_price=_to_decimal(r.get(col_price) or ""),
            old_price=_to_decimal(r.get(col_oldpri) or ""),
            confidence_label=conf or None,
            confidence_level=level,
            source_file=source_file_name or str(csv_path),
            source_row_number=row_num,
            review_status="pending",
        )
        objects.append(obj)
        stats.total += 1
        if level == "高可信":
            stats.high += 1
        elif level == "中可信":
            stats.mid += 1
        else:
            stats.low += 1

    db.bulk_save_objects(objects)
    db.flush()
    return stats


# ──────────────────────────────────────────────
# 3. 客户料号严格提取
# ──────────────────────────────────────────────

_PRODUCT_KEYWORDS: frozenset[str] = frozenset({
    "纸箱", "外箱", "内箱", "内盒", "外盒", "盒子",
    "垫片", "隔板", "衬板", "衬纸", "面纸",
    "托盘", "卡板", "底托", "护角",
    "满衬板", "蜂窝板", "蜂窝纸板",
    "盖板", "底板", "中板", "拼版",
})

_FORBIDDEN_EXACT: frozenset[str] = frozenset({
    "cartonbox", "box", "carton",
    "inner", "outer", "pkg", "package", "label",
})


def is_strict_code(s: str) -> bool:
    """判断字符串是否像客户料号（严格规则）。"""
    s = s.strip()
    if not s or len(s) < 2 or len(s) > 30:
        return False
    # 不能以中文字符开头
    if "一" <= s[0] <= "鿿":
        return False
    # 不能包含中文箱型品名关键词
    for kw in _PRODUCT_KEYWORDS:
        if kw in s:
            return False
    # 不能是已知英文品名词
    if s.lower() in _FORBIDDEN_EXACT:
        return False
    # 纯数字且 ≥ 5 位 → 料号（如 21311095）
    if re.fullmatch(r"\d{5,}", s):
        return True
    # 含英文字母 + 含数字 → 料号（如 A113B, WCX1, K617K）
    has_alpha = bool(re.search(r"[A-Za-z]", s))
    has_digit = bool(re.search(r"\d", s))
    if has_alpha and has_digit:
        return True
    return False


def extract_customer_code(product_name: str) -> tuple[str, str] | None:
    """从品名中严格提取客户料号。

    Returns (extracted_code, rule) 或 None（无法提取）。
    rule = "slash" | "space"
    """
    if not product_name:
        return None
    name = product_name.strip()
    if "/" in name:
        candidate = name.split("/")[0].strip()
        rule = "slash"
    elif " " in name:
        candidate = name.split(" ")[0].strip()
        rule = "space"
    else:
        return None  # 无分隔符，不处理

    if not is_strict_code(candidate):
        return None
    # 提取结果不得与原品名完全相同（意味着无法分割）
    if candidate == name:
        return None
    return candidate, rule


# ──────────────────────────────────────────────
# 4. 客户料号批量规范（预览）
# ──────────────────────────────────────────────

@dataclass
class CustomerCodePreview:
    total: int = 0
    will_update: int = 0
    skipped: int = 0
    samples_update: list[dict] = field(default_factory=list)
    samples_skip: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    write_field: str = "customer_material_code"
    legacy_field: str = "legacy_customer_material_code"


def preview_customer_code_updates(db: "Session") -> CustomerCodePreview:
    """只读预览：返回将被更新和跳过的产品清单（不写库）。"""
    from app.models.product import Product
    from sqlalchemy import select

    preview = CustomerCodePreview()

    products = db.scalars(
        select(Product).where(Product.deleted_at.is_(None))
    ).all()
    preview.total = len(products)

    # 建立 customer_id → 已有 customer_material_code 集合（用于唯一性检查）
    from collections import defaultdict
    cmc_by_customer: dict[int, set[str]] = defaultdict(set)
    for p in products:
        cmc_by_customer[p.customer_id].add(p.customer_material_code or "")

    for p in products:
        result = extract_customer_code(p.product_name or "")
        if result is None:
            preview.skipped += 1
            if len(preview.samples_skip) < 50:
                preview.samples_skip.append({
                    "id": p.id,
                    "product_name": p.product_name,
                    "customer_material_code": p.customer_material_code,
                    "reason": "无法提取",
                })
            continue

        candidate, rule = result
        current_cmc = p.customer_material_code or ""

        # 已有相同值 → 跳过
        if candidate == current_cmc:
            preview.skipped += 1
            if len(preview.samples_skip) < 50:
                preview.samples_skip.append({
                    "id": p.id,
                    "product_name": p.product_name,
                    "customer_material_code": current_cmc,
                    "reason": "已一致，跳过",
                })
            continue

        # 新值在同客户下已被占用（另一产品） → 跳过
        others_cmc = cmc_by_customer[p.customer_id] - {current_cmc}
        if candidate in others_cmc:
            preview.skipped += 1
            if len(preview.samples_skip) < 50:
                preview.samples_skip.append({
                    "id": p.id,
                    "product_name": p.product_name,
                    "customer_material_code": current_cmc,
                    "reason": f"与同客户其他产品冲突: {candidate!r}",
                })
            continue

        preview.will_update += 1
        updates = {"customer_material_code": candidate}
        if not p.legacy_customer_material_code:
            updates["legacy_customer_material_code"] = current_cmc
        preview.changes.append({
            "key": f"product:{p.id}",
            "object_type": "product",
            "object_id": p.id,
            "expected_version": int(p.version),
            "updates": updates,
            "old": current_cmc,
            "new": candidate,
            "rule": rule,
        })
        if len(preview.samples_update) < 50:
            preview.samples_update.append({
                "id": p.id,
                "product_name": p.product_name,
                "old_cmc": current_cmc,
                "new_cmc": candidate,
                "rule": rule,
            })

    return preview


# ──────────────────────────────────────────────
# 5. 客户料号批量写入
# ──────────────────────────────────────────────

@dataclass
class CustomerCodeWriteResult:
    updated: int = 0
    skipped: int = 0
    changes: list[dict] = field(default_factory=list)


def apply_customer_code_updates(
    db: "Session",
    *,
    user: "User | None" = None,
    confirmation_tokens: Mapping[str, str] | None = None,
    preview_confirmed: bool = False,
) -> CustomerCodeWriteResult:
    """在事务中按版本服务批量更新 customer_material_code。"""
    from app.models.product import Product
    from sqlalchemy import select

    user = _version_actor(user)
    result = CustomerCodeWriteResult()
    preview = preview_customer_code_updates(db)
    result.skipped = preview.skipped
    product_ids = [item["object_id"] for item in preview.changes]
    products = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(product_ids))
        ).all()
    } if product_ids else {}

    for item in preview.changes:
        p = products[item["object_id"]]
        _apply_batch_versioned_update(
            db,
            object_type="product",
            entity=p,
            updates=item["updates"],
            expected_version=item["expected_version"],
            user=user,
            reason="系统批量规范客户料号",
            source="system.material-mapping.apply-customer-codes",
            action="update",
            confirmation_token=_confirmation_token(
                confirmation_tokens,
                "product",
                p.id,
            ),
            preview_confirmed=preview_confirmed,
        )
        result.updated += 1
        result.changes.append({
            "id": p.id,
            "old": item["old"],
            "new": item["new"],
            "rule": item["rule"],
        })

    db.flush()
    return result


# ──────────────────────────────────────────────
# 6. 高可信材质映射写入
# ──────────────────────────────────────────────

def _clean_legacy_to_code(legacy_text: str) -> list[str]:
    """从 legacy_material_text 提取可能的旧代码候选列表。"""
    if not legacy_text:
        return []
    t = legacy_text.strip()
    candidates = [t]
    # 去掉开头数字序号（如 "010 K515R" → "K515R"）
    stripped = re.sub(r"^\d+\s+", "", t)
    if stripped and stripped != t:
        candidates.append(stripped)
    # 首个 token
    parts = t.split()
    if len(parts) >= 2:
        candidates.append(parts[0])
        candidates.append(parts[1])
    return list(dict.fromkeys(candidates))


@dataclass
class MaterialMappingResult:
    products_updated: int = 0
    materials_created: int = 0
    materials_reused: int = 0
    skipped_no_match: int = 0
    skipped_already_set: int = 0
    changes: list[dict] = field(default_factory=list)


_MATERIAL_DICTIONARY_LAYER_COUNTS = frozenset({3, 5, 7})


def _validated_material_code_and_layer(
    new_code: str | None,
    layer_value: str | None,
) -> tuple[str, int] | None:
    """Validate a dictionary code without introducing a business flute dimension."""
    code = str(new_code or "").strip().upper()
    if (
        len(code) not in _MATERIAL_DICTIONARY_LAYER_COUNTS
        or re.fullmatch(r"[A-Z0-9]+", code) is None
    ):
        return None

    layer_match = re.search(r"(\d+)", str(layer_value or ""))
    layer_count = int(layer_match.group(1)) if layer_match else len(code)
    if layer_count not in _MATERIAL_DICTIONARY_LAYER_COUNTS:
        return None
    if len(code) != layer_count:
        return None
    return code, layer_count


@dataclass
class MaterialMappingPreview:
    products_updated: int = 0
    materials_created: int = 0
    materials_reused: int = 0
    skipped_no_match: int = 0
    skipped_already_set: int = 0
    changes: list[dict] = field(default_factory=list)


def preview_high_confidence_material_mapping(
    db: "Session",
    csv_path: Path,
) -> MaterialMappingPreview:
    """Build the exact semantic plan that must be confirmed before apply."""

    from collections import defaultdict
    from app.models.material import Material
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from app.models.product import Product
    from sqlalchemy import select

    del csv_path  # Candidates are persisted; the path remains API-compatible.
    preview = MaterialMappingPreview()
    candidates = db.scalars(
        select(MaterialCodeMappingCandidate).where(
            MaterialCodeMappingCandidate.confidence_level == "高可信",
            MaterialCodeMappingCandidate.new_code.isnot(None),
        )
    ).all()
    code_to_candidates: dict[str, list[MaterialCodeMappingCandidate]] = defaultdict(list)
    for candidate in candidates:
        code_to_candidates[candidate.old_code].append(candidate)

    unique_mapping: dict[
        str, tuple[MaterialCodeMappingCandidate, str, int]
    ] = {}
    for old_code, grouped in code_to_candidates.items():
        valid_candidates = [
            (candidate, validated)
            for candidate in grouped
            if (
                validated := _validated_material_code_and_layer(
                    candidate.new_code,
                    candidate.layer_count,
                )
            )
            is not None
        ]
        targets = {validated for _, validated in valid_candidates}
        if len(targets) == 1:
            candidate, (new_code, layer_count) = valid_candidates[0]
            unique_mapping[old_code] = (candidate, new_code, layer_count)

    existing_materials = {
        material.code.strip().upper(): material
        for material in db.scalars(select(Material)).all()
    }
    available_codes = set(existing_materials)
    planned_keys: set[str] = set()
    products = db.scalars(
        select(Product).where(
            Product.deleted_at.is_(None),
            Product.material_id.is_(None),
            Product.legacy_material_text.isnot(None),
        )
    ).all()

    for product in products:
        matched = next(
            (
                unique_mapping[code]
                for code in _clean_legacy_to_code(product.legacy_material_text or "")
                if code in unique_mapping
            ),
            None,
        )
        if matched is None:
            preview.skipped_no_match += 1
            continue
        candidate, new_code, layer_count = matched
        material = existing_materials.get(new_code)
        if material is not None and material.layer_count not in (None, layer_count):
            preview.skipped_no_match += 1
            continue

        if new_code not in available_codes:
            create_key = f"material-create:{new_code}"
            preview.changes.append({
                "key": create_key,
                "object_type": "material",
                "object_id": None,
                "create": {
                    "code": new_code,
                    "layer_count": layer_count,
                    "supplier_name": "嘉林亿",
                    "basis_weight_description": candidate.weight_structure,
                    "quote_price": candidate.new_price,
                },
            })
            planned_keys.add(create_key)
            available_codes.add(new_code)
            preview.materials_created += 1
        else:
            preview.materials_reused += 1

        if material is not None:
            updates = {
                field_name: value
                for field_name, value in {
                    "layer_count": layer_count,
                    "flute_type": None,
                }.items()
                if getattr(material, field_name) != value
            }
            material_key = f"material:{material.id}"
            if updates and material_key not in planned_keys:
                preview.changes.append({
                    "key": material_key,
                    "object_type": "material",
                    "object_id": material.id,
                    "expected_version": int(material.version),
                    "updates": updates,
                })
                planned_keys.add(material_key)

        preview.changes.append({
            "key": f"product:{product.id}",
            "object_type": "product",
            "object_id": product.id,
            "expected_version": int(product.version),
            "updates": {"material_code": new_code},
            "candidate_id": candidate.id,
            "matched_old_code": candidate.old_code,
        })
        preview.products_updated += 1

    return preview


def apply_high_confidence_material_mapping(
    db: "Session",
    csv_path: Path,
    *,
    user: "User | None" = None,
    confirmation_tokens: Mapping[str, str] | None = None,
    preview_confirmed: bool = False,
) -> MaterialMappingResult:
    """
    高可信材质映射直接写入 products.material_id。

    条件（全部满足）：
    - CSV 中高可信（可直接替换 / 差0g / 差5g 无推定）
    - 旧代码唯一映射到新代码（无冲突）
    - product.material_id 当前为 NULL
    - product.legacy_material_text 能匹配到旧代码
    - 可找到或安全创建对应的嘉林亿材质字典
    """
    from app.models.material_mapping import MaterialCodeMappingCandidate
    from app.models.material import Material
    from app.models.product import Product
    from app.services.master_data_versioning import record_versioned_create
    from sqlalchemy import select

    user = _version_actor(user)
    result = MaterialMappingResult()

    # 从候选表中取高可信唯一映射
    candidates = db.scalars(
        select(MaterialCodeMappingCandidate).where(
            MaterialCodeMappingCandidate.confidence_level == "高可信",
            MaterialCodeMappingCandidate.new_code.isnot(None),
        )
    ).all()

    # 建立 old_code → candidate 映射（只取唯一映射）
    from collections import defaultdict
    code_to_candidates: dict[str, list[MaterialCodeMappingCandidate]] = defaultdict(list)
    for c in candidates:
        code_to_candidates[c.old_code].append(c)
    # 过滤冲突（同旧代码多个不同新代码）
    unique_mapping: dict[
        str, tuple[MaterialCodeMappingCandidate, str, int]
    ] = {}
    for old_code, cands in code_to_candidates.items():
        valid_candidates = [
            (candidate, validated)
            for candidate in cands
            if (
                validated := _validated_material_code_and_layer(
                    candidate.new_code,
                    candidate.layer_count,
                )
            )
            is not None
        ]
        new_codes = {validated for _, validated in valid_candidates}
        if len(new_codes) == 1:
            candidate, (new_code, layer_count) = valid_candidates[0]
            unique_mapping[old_code] = (candidate, new_code, layer_count)

    if not unique_mapping:
        return result

    # 预加载 materials 字典
    existing_materials: dict[str, Material] = {
        m.code.strip().upper(): m for m in db.scalars(select(Material)).all()
    }

    # 遍历需要更新的产品
    products = db.scalars(
        select(Product).where(
            Product.deleted_at.is_(None),
            Product.material_id.is_(None),
            Product.legacy_material_text.isnot(None),
        )
    ).all()

    for p in products:
        candidates_for_product = _clean_legacy_to_code(p.legacy_material_text or "")
        matched_mapping: tuple[MaterialCodeMappingCandidate, str, int] | None = None
        for code_cand in candidates_for_product:
            if code_cand in unique_mapping:
                matched_mapping = unique_mapping[code_cand]
                break

        if matched_mapping is None:
            result.skipped_no_match += 1
            continue
        matched_cand, new_code, layer_count_int = matched_mapping

        # product.material_id 已经设置 → 跳过
        if p.material_id is not None:
            result.skipped_already_set += 1
            continue

        # 找或创建材质记录
        mat = existing_materials.get(new_code)
        if mat is None:
            # 创建新材质记录
            mat = Material(
                code=new_code,
                supplier_name="嘉林亿",
                layer_count=layer_count_int,
                flute_type=None,
                basis_weight_description=matched_cand.weight_structure,
                quote_price=matched_cand.new_price,
                price_unit="元/㎡",
                remarks=(
                    f"由 cowork CSV 自动导入；"
                    f"旧代码: {matched_cand.old_code}；"
                    f"原供应商: {matched_cand.old_supplier}；"
                    f"可信度: {matched_cand.confidence_label}"
                ),
                is_active=True,
            )
            db.add(mat)
            db.flush()  # 获取 mat.id
            record_versioned_create(
                db,
                object_type="material",
                entity=mat,
                user=user,
                reason="系统高可信材质映射批次创建材质",
                source="system.material-mapping.apply-high-confidence",
            )
            existing_materials[new_code] = mat
            result.materials_created += 1
        else:
            if mat.layer_count not in (None, layer_count_int):
                result.skipped_no_match += 1
                continue
            material_updates = {
                field_name: value
                for field_name, value in {
                    "layer_count": layer_count_int,
                    "flute_type": None,
                }.items()
                if getattr(mat, field_name) != value
            }
            if material_updates:
                _apply_batch_versioned_update(
                    db,
                    object_type="material",
                    entity=mat,
                    updates=material_updates,
                    expected_version=int(mat.version),
                    user=user,
                    reason="系统高可信材质映射批次更新材质",
                    source="system.material-mapping.apply-high-confidence",
                    action="update",
                    confirmation_token=_confirmation_token(
                        confirmation_tokens,
                        "material",
                        mat.id,
                    ),
                    preview_confirmed=preview_confirmed,
                )
            result.materials_reused += 1

        _apply_batch_versioned_update(
            db,
            object_type="product",
            entity=p,
            updates={"material_id": mat.id},
            expected_version=int(p.version),
            user=user,
            reason="系统高可信材质映射批次更新常用箱",
            source="system.material-mapping.apply-high-confidence",
            action="update",
            confirmation_token=_confirmation_token(
                confirmation_tokens,
                "product",
                p.id,
            ),
            preview_confirmed=preview_confirmed,
        )
        result.products_updated += 1
        result.changes.append({
            "product_id": p.id,
            "legacy_text": p.legacy_material_text,
            "matched_old_code": matched_cand.old_code,
            "new_material_code": new_code,
            "new_material_id": mat.id,
            "confidence": matched_cand.confidence_label,
        })

        # 更新候选表审批状态
        matched_cand.review_status = "approved"
        matched_cand.review_note = f"高可信自动写入 product_id={p.id}"

    db.flush()
    return result
