from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today
from app.models.material_price_history import MaterialPriceAdjustmentBatch
from app.models.supplier_material_rule import (
    SupplierMaterialBasePrice,
    SupplierMaterialSubstitutionRule,
)


MONEY = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _base_price(
    db: Session, supplier_name: str, material_code: str
) -> SupplierMaterialBasePrice | None:
    return db.scalar(
        select(SupplierMaterialBasePrice)
        .where(
            SupplierMaterialBasePrice.supplier_name == supplier_name,
            SupplierMaterialBasePrice.material_code == material_code,
            SupplierMaterialBasePrice.is_active.is_(True),
        )
        .order_by(
            SupplierMaterialBasePrice.effective_date.desc(),
            SupplierMaterialBasePrice.id.desc(),
        )
    )


def _delta(
    db: Session,
    supplier_name: str,
    rule_type: str,
    from_code: str,
    to_code: str,
) -> SupplierMaterialSubstitutionRule | None:
    if from_code == to_code:
        return None
    return db.scalar(
        select(SupplierMaterialSubstitutionRule)
        .where(
            SupplierMaterialSubstitutionRule.supplier_name == supplier_name,
            SupplierMaterialSubstitutionRule.rule_type == rule_type,
            SupplierMaterialSubstitutionRule.from_code == from_code,
            SupplierMaterialSubstitutionRule.to_code == to_code,
            SupplierMaterialSubstitutionRule.is_active.is_(True),
        )
        .order_by(
            SupplierMaterialSubstitutionRule.effective_date.desc(),
            SupplierMaterialSubstitutionRule.id.desc(),
        )
    )


def latest_adjustment(db: Session, supplier_name: str) -> dict:
    row = db.scalar(
        select(MaterialPriceAdjustmentBatch)
        .where(MaterialPriceAdjustmentBatch.supplier_name == supplier_name)
        .order_by(
            MaterialPriceAdjustmentBatch.effective_date.desc(),
            MaterialPriceAdjustmentBatch.id.desc(),
        )
    )
    return {
        "percent": Decimal(row.adjust_percent or 0) if row else Decimal("0"),
        "effective_date": row.effective_date if row else None,
        "batch_id": row.id if row else None,
    }


def estimate_material_price(
    db: Session,
    *,
    supplier_name: str,
    material_code: str,
    usage_flute_type: str | None = None,
) -> dict:
    code = material_code.strip().upper()
    if len(code) not in {3, 5}:
        return {"calculable": False, "message": "材质代码需为3位或5位"}

    steps: list[dict] = []
    if len(code) == 3:
        reference_code = f"{code[0]}4{code[2]}"
        base = _base_price(db, supplier_name, reference_code)
        if base is None:
            return {
                "calculable": False,
                "reference_code": reference_code,
                "message": "找不到三层基础报价，无法自动推算平方价",
            }
        raw_base = Decimal(base.base_price)
        rule = _delta(db, supplier_name, "corrugated_b", "4", code[1])
        if code[1] != "4" and rule is None:
            return {
                "calculable": False,
                "reference_code": reference_code,
                "message": f"缺少B/E楞瓦纸4换{code[1]}加价规则",
            }
        if rule:
            raw_base += Decimal(rule.price_delta)
            steps.append(
                {
                    "position": 2,
                    "role": "瓦楞纸",
                    "rule": f"4换{code[1]}",
                    "delta": rule.price_delta,
                }
            )
        usage_delta = Decimal("0.04") if (usage_flute_type or "").upper() == "A" else Decimal("0")
    else:
        reference_code = f"{code[0]}414{code[4]}"
        base = _base_price(db, supplier_name, reference_code)
        if base is None:
            return {
                "calculable": False,
                "reference_code": reference_code,
                "message": "找不到五层基础报价，无法自动推算平方价",
            }
        raw_base = Decimal(base.base_price)
        for position, rule_type, from_code, to_code, role in (
            (2, "corrugated_b", "4", code[1], "B楞瓦纸"),
            (3, "core", "1", code[2], "芯纸"),
            (4, "corrugated_a", "4", code[3], "A楞瓦纸"),
        ):
            rule = _delta(db, supplier_name, rule_type, from_code, to_code)
            if from_code != to_code and rule is None:
                return {
                    "calculable": False,
                    "reference_code": reference_code,
                    "message": f"缺少{role}{from_code}换{to_code}加价规则",
                }
            if rule:
                raw_base += Decimal(rule.price_delta)
                steps.append(
                    {
                        "position": position,
                        "role": role,
                        "rule": f"{from_code}换{to_code}",
                        "delta": rule.price_delta,
                    }
                )
        usage_delta = Decimal("0")

    base_price = _money(raw_base)
    usage_base_price = _money(raw_base + usage_delta)
    adjustment = latest_adjustment(db, supplier_name)
    current = _money(
        usage_base_price
        * (Decimal("1") + adjustment["percent"] / Decimal("100"))
    )
    source_date = base.effective_date if base else beijing_today()
    return {
        "calculable": True,
        "material_code": code,
        "reference_code": reference_code,
        "quotation_base_price": base_price,
        "usage_base_price": usage_base_price,
        "usage_flute_type": (usage_flute_type or "").upper() or None,
        "usage_flute_delta": usage_delta,
        "current_suggested_price": current,
        "base_price_date": source_date,
        "adjustment_percent": adjustment["percent"],
        "adjustment_effective_date": adjustment["effective_date"],
        "adjustment_batch_id": adjustment["batch_id"],
        "steps": steps,
        "message": "已按供应商报价基准和当前调价规则推算",
    }
