from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.models.customer_material import CustomerMaterialCandidate
from app.models.material import Material


def normalize_material_candidate_key(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return re.sub(r"\s+", "", normalized)


def normalize_supplier_candidate_key(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return re.sub(r"\s+", "", normalized)


def reference_price(material: Material) -> Decimal | None:
    return material.quote_price if material.quote_price is not None else material.rule_base_price


def candidate_response(
    candidate: CustomerMaterialCandidate,
    material: Material,
    *,
    history_count: int = 0,
    last_used_at: datetime | None = None,
    recommended: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    if candidate.manual_priority > 0:
        reasons.append(f"人工优先级 {candidate.manual_priority}")
    if history_count > 0:
        reasons.append(f"该客户历史选择 {history_count} 次")
    if last_used_at is not None:
        reasons.append("该客户近期使用过")
    price = reference_price(material)
    if price is not None:
        reasons.append("有当前参考价")
    if not reasons:
        reasons.append("客户原始材质代码匹配")
    return {
        "candidate_id": candidate.id,
        "customer_id": candidate.customer_id,
        "original_material_code": candidate.original_material_code,
        "material_id": material.id,
        "material_code": material.code,
        "actual_material_code": material.code,
        "supplier_name": material.supplier_name or candidate.supplier_name,
        "layer_count": material.layer_count,
        "flute_type": material.flute_type,
        "reference_price": price,
        "price_unit": material.price_unit,
        "history_count": history_count,
        "last_used_at": last_used_at,
        "manual_priority": candidate.manual_priority,
        "is_active": candidate.is_active,
        "source": candidate.source,
        "notes": candidate.notes,
        "recommended": recommended,
        "recommendation_reasons": reasons,
        "recommendation_reason": "；".join(reasons),
    }
