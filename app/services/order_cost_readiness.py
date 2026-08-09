from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


COST_GAP_CATEGORIES: tuple[dict[str, str], ...] = (
    {"code": "supplier_price", "label": "供应商价格"},
    {"code": "report_dimensions", "label": "报料尺寸"},
    {"code": "supplier_material", "label": "材质资料"},
    {"code": "processing_rule", "label": "加工规则"},
    {"code": "printing_colors", "label": "印刷颜色"},
    {"code": "external_purchase_cost", "label": "外购件价格"},
    {"code": "required_quantity", "label": "需求数量"},
    {"code": "material_snapshot", "label": "材料成本快照"},
    {"code": "other", "label": "其他资料"},
)

_CATEGORY_LABELS = {item["code"]: item["label"] for item in COST_GAP_CATEGORIES}


def load_cost_missing_items(value: object) -> list[str]:
    """Read one immutable snapshot's missing items without trusting its JSON."""

    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    result = [str(item).strip() for item in parsed if str(item).strip()]
    return list(dict.fromkeys(result)) or ["成本资料需要核对"]


def classify_cost_gap(missing_item: object) -> dict[str, str]:
    text = str(missing_item or "").strip()
    if "缺少有效平方成本" in text:
        code = "supplier_price"
    elif "缺少报料长宽" in text:
        code = "report_dimensions"
    elif "缺少有效供应商材质" in text:
        code = "supplier_material"
    elif "加工费规则待完善" in text:
        code = "processing_rule"
    elif "印刷颜色数量待确认" in text:
        code = "printing_colors"
    elif "外购件采购成本未纳入" in text:
        code = "external_purchase_cost"
    elif "需求数量无效" in text:
        code = "required_quantity"
    elif "材料成本快照缺失" in text:
        code = "material_snapshot"
    else:
        code = "other"
    return {"code": code, "label": _CATEGORY_LABELS[code]}


def classify_cost_gaps(missing_items: Iterable[object]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for missing_item in missing_items:
        text = str(missing_item or "").strip()
        category = classify_cost_gap(text)
        row = grouped.setdefault(
            category["code"],
            {**category, "details": []},
        )
        if text and text not in row["details"]:
            row["details"].append(text)
    if not grouped:
        grouped["other"] = {
            "code": "other",
            "label": _CATEGORY_LABELS["other"],
            "details": ["成本资料需要核对"],
        }
    category_order = {item["code"]: index for index, item in enumerate(COST_GAP_CATEGORIES)}
    return sorted(grouped.values(), key=lambda item: category_order[item["code"]])
