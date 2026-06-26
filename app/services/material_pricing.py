"""统一材料价格 / 楞型加价 / 比价服务（v0.19.2-B）。

核心：材质主数据里的「平方报价」是基础价（供应商报价单 B/E 同价）。
产品实际楞型（A/B/E/AB/BE）来自常用箱/订单，可能需要在基础价上加价：
    最终材料平方价 = 基础平方报价 + 楞型加价(price_delta)

常用箱成本参考、订单预估成本、比价统一调用 get_effective_material_price，
保证口径一致。规则存于 supplier_flute_price_rules，可维护、可停用。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.material import Material
from app.models.supplier_flute_price_rule import SupplierFlutePriceRule


def _norm_flute(flute_type: str | None) -> str:
    return (flute_type or "").strip().upper()


def get_flute_delta(
    session: Session,
    *,
    supplier_name: str | None,
    layer_count: int | None,
    flute_type: str | None,
) -> tuple[Decimal, int | None]:
    """查楞型加价：返回 (price_delta, rule_id)。无匹配规则返回 (0, None)。

    取 supplier_name + layer_count + flute_type 完全匹配、启用、最新生效的一条。
    """
    flute = _norm_flute(flute_type)
    if not supplier_name or layer_count is None or not flute:
        return Decimal("0"), None
    stmt = (
        select(SupplierFlutePriceRule)
        .where(
            SupplierFlutePriceRule.supplier_name == supplier_name,
            SupplierFlutePriceRule.layer_count == layer_count,
            SupplierFlutePriceRule.flute_type == flute,
            SupplierFlutePriceRule.is_active.is_(True),
        )
        .order_by(
            SupplierFlutePriceRule.effective_date.is_(None).asc(),
            SupplierFlutePriceRule.effective_date.desc(),
            SupplierFlutePriceRule.id.desc(),
        )
    )
    rule = session.scalars(stmt).first()
    if rule is None:
        return Decimal("0"), None
    return Decimal(rule.price_delta or 0), rule.id


def get_effective_material_price(
    session: Session,
    *,
    material: Material | None = None,
    base_price: Decimal | None = None,
    supplier_name: str | None = None,
    layer_count: int | None = None,
    flute_type: str | None = None,
) -> dict:
    """统一计算最终材料平方价。

    传 material 时取 material.quote_price 为基础价、并默认沿用其 supplier/layer；
    也可直接传 base_price/supplier_name/layer_count。flute_type 是产品实际楞型。
    返回 {base_price, flute_delta, effective_price, rule_id}（None 价用 None）。
    """
    if material is not None:
        if base_price is None:
            base_price = material.quote_price
        if supplier_name is None:
            supplier_name = material.supplier_name
        if layer_count is None:
            layer_count = material.layer_count

    delta, rule_id = get_flute_delta(
        session,
        supplier_name=supplier_name,
        layer_count=layer_count,
        flute_type=flute_type,
    )

    base = None if base_price is None else Decimal(base_price)
    effective = None if base is None else (base + delta)
    return {
        "base_price": None if base is None else float(base),
        "flute_delta": float(delta),
        "effective_price": None if effective is None else float(effective),
        "rule_id": rule_id,
        "supplier_name": supplier_name,
        "layer_count": layer_count,
        "flute_type": _norm_flute(flute_type) or None,
    }


# ---------------------------------------------------------------------------
# 比价
# ---------------------------------------------------------------------------

# 进口/特殊材质关键字：命中则标「仅供参考」，不直接换算。
_SPECIAL_KEYWORDS = ("俄卡", "美卡", "进口", "木浆", "白卡", "牛卡进口")


def classify_material(material: Material) -> dict:
    """从 paper_composition / code 粗分类别，绝不因字段缺失抛错。"""
    paper = (material.paper_composition or "").strip()
    code = (material.code or "").strip()
    text = paper or code
    special = any(k in text for k in _SPECIAL_KEYWORDS)
    # 等级：A级/高强等粗标签（缺失则「未标注」）
    grade = "未标注"
    for g in ("A级", "B级", "C级", "高强"):
        if g in text:
            grade = g
            break
    origin = "进口" if ("进口" in text or "俄卡" in text or "美卡" in text) else "国产"
    return {"special": special, "grade": grade, "origin": origin, "text": text}


def _weight_key(material: Material) -> str:
    return (material.basis_weight_description or "").strip()


def compare_materials(
    session: Session,
    *,
    candidates: Iterable[Material],
    layer_count: int | None,
    flute_type: str | None,
) -> list[dict]:
    """对一组候选材质按「层数 + 纯克重结构」分组比价，含楞型加价后的最终可比价。

    每组返回最低/最高、各供应商行（基础价/加价/最终价/与最低价差/状态）。
    绝不因 paper_composition 等字段缺失而抛错。
    """
    groups: dict[str, list[Material]] = {}
    for m in candidates:
        if m.quote_price is None:
            continue
        key = f"{m.layer_count or ''}|{_weight_key(m)}"
        groups.setdefault(key, []).append(m)

    out: list[dict] = []
    for key, mats in groups.items():
        rows = []
        for m in mats:
            eff = get_effective_material_price(
                session,
                material=m,
                flute_type=flute_type,
                layer_count=m.layer_count,
            )
            cls = classify_material(m)
            rows.append(
                {
                    "material_id": m.id,
                    "material_code": (m.code or "").split("-")[0],
                    "supplier_name": m.supplier_name,
                    "base_price": eff["base_price"],
                    "flute_delta": eff["flute_delta"],
                    "effective_price": eff["effective_price"],
                    "quote_date": m.quote_date.isoformat() if m.quote_date else None,
                    "weight_structure": _weight_key(m),
                    "special": cls["special"],
                    "grade": cls["grade"],
                    "origin": cls["origin"],
                }
            )
        eff_vals = [r["effective_price"] for r in rows if r["effective_price"] is not None]
        if not eff_vals:
            continue
        min_eff = min(eff_vals)
        for r in rows:
            r["delta_to_min"] = (
                None
                if r["effective_price"] is None
                else round(r["effective_price"] - min_eff, 4)
            )
        rows.sort(key=lambda r: (r["effective_price"] is None, r["effective_price"] or 0))

        # 跨类别提示
        classes = {(r["grade"], r["origin"]) for r in rows}
        any_special = any(r["special"] for r in rows)
        if any_special:
            status = "仅供参考"
        elif len(classes) > 1:
            status = "材质类别不同，仅供参考"
        elif len(rows) < 2:
            status = "仅一家供应商"
        else:
            status = "可比价"

        layer = key.split("|")[0]
        weight = key.split("|", 1)[1]
        out.append(
            {
                "key": key,
                "layer_count": int(layer) if layer.isdigit() else None,
                "weight_structure": weight,
                "flute_type": _norm_flute(flute_type) or None,
                "status": status,
                "min_effective": min_eff,
                "max_effective": max(eff_vals),
                "rows": rows,
            }
        )
    out.sort(key=lambda g: g["key"])
    return out
