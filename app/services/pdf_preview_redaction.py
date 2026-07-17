"""Pure response-body redaction for PDF order previews.

The PDF parser intentionally returns both customer-facing order facts and
internal matching/cost references.  This module only shapes that return body;
it never changes the parsed draft or performs permission checks.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


# Customer/PDF sales prices are deliberately allowed.  Do not use a blanket
# ``"price" in key`` rule: unit_price and product_default_price are order
# facts that the PDF-preview UI must still show.
SAFE_CUSTOMER_PRICE_KEYS = frozenset(
    {
        "unit_price",
        "product_default_price",
        "customer_unit_price",
        "sale_unit_price",
        "sale_unit_price_no_tax",
        "pdf_price",
        "final_unit_price",
        "suggested_unit_price",
        "default_unit_price",
    }
)

# Fields currently emitted by the PDF-preview matching pipeline, plus stable
# names used by nearby cost-preview responses.  These are removed at every
# nesting level, so newly nested response structures remain protected.
EXACT_SENSITIVE_KEYS = frozenset(
    {
        "estimated_cost",
        "estimated_cost_status",
        "unit_estimated_cost",
        "total_estimated_cost",
        "estimated_gross_profit",
        "cost_status",
        "cost_candidates",
        "cost_candidate",
        "cost_price",
        "cost_unit_price",
        "material_cost",
        "board_cost",
        "board_price",
        "board_square_price",
        "material_square_price",
        "supplier_square_price",
        "supplier_price",
        "supplier_quote_price",
        "quote_price",
        "effective_price",
        "price_delta",
        "price_components",
        "price_composition",
        "price_breakdown",
        "price_structure",
        "pricing_components",
        "pricing_breakdown",
        "价格构成",
        "价格组成",
        "成本候选",
        "材料平方价",
        "供应商报价",
        "供应商采购价",
    }
)

# Broad fragments cover equivalent names from future parser enrichments while
# leaving ordinary customer ``*price`` fields alone.
SENSITIVE_KEY_FRAGMENTS = (
    "cost",
    "成本",
    "gross_profit",
    "gross_margin",
    "毛利",
    "利润",
    "material_square",
    "材料平方",
    "供应商报价",
    "供应商采购",
    "价格构成",
    "price_component",
    "price_composition",
    "price_breakdown",
)

_PRICE_WORDS = ("price", "quote", "报价", "采购价", "平方价")
_SUPPLIER_PRICE_CONTEXTS = ("supplier", "material", "board", "供应商", "材料", "纸板")


def redact_pdf_preview_for_user(draft: Any, can_view_cost: bool) -> Any:
    """Return an independent PDF-preview body suitable for one user.

    ``draft`` is normally a JSON-compatible mapping, but arbitrary nested
    lists/dicts are traversed as a fail-safe.  Call this once per draft for a
    batch response.  The source object is never mutated, including when the
    caller is permitted to view costs.
    """
    copied = deepcopy(draft)
    if can_view_cost:
        return copied
    return _redact_value(copied)


def _redact_value(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_value(child, parent_key=str(key))
            for key, child in value.items()
            if not _is_sensitive_key(str(key), parent_key=parent_key)
        }
    if isinstance(value, list):
        return [_redact_value(child, parent_key=parent_key) for child in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(child, parent_key=parent_key) for child in value)
    if parent_key == "standard_material_label" and isinstance(value, str):
        return _without_material_quote(value)
    return value


def _is_sensitive_key(key: str, *, parent_key: str | None = None) -> bool:
    normalized = _normalize_key(key)
    if normalized in SAFE_CUSTOMER_PRICE_KEYS:
        return False
    if normalized in EXACT_SENSITIVE_KEYS:
        return True
    if any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS):
        return True
    normalized_parent = _normalize_key(parent_key or "")
    if (
        any(context in normalized_parent for context in _SUPPLIER_PRICE_CONTEXTS)
        and any(word in normalized for word in _PRICE_WORDS)
    ):
        return True
    return (
        any(context in normalized for context in _SUPPLIER_PRICE_CONTEXTS)
        and any(word in normalized for word in _PRICE_WORDS)
    )


def _normalize_key(key: str) -> str:
    return key.casefold().replace("-", "_").replace(" ", "_")


def _without_material_quote(label: str) -> str:
    """Remove only the final quote segment from the standard material label.

    The current generator uses ``code｜supplier｜weight｜quote``.  Supporting
    both full-width and ASCII separators keeps this safe for historical and
    test payloads without touching product, supplier, or material evidence.
    """
    for separator in ("｜", "|"):
        if separator not in label:
            continue
        head, tail = label.rsplit(separator, 1)
        if _looks_like_price(tail):
            return head.rstrip()
    return label


def _looks_like_price(value: str) -> bool:
    stripped = value.strip().casefold()
    if stripped in {"", "-"}:
        return False
    for prefix in ("¥", "￥", "rmb", "cny"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
            break
    try:
        float(stripped.replace(",", ""))
    except ValueError:
        return False
    return True
