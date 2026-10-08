from __future__ import annotations

import json
from collections.abc import Mapping


LABEL_OVERRIDE_FIELD_LIMITS = {
    "display_identity": 200,
    "product_name": 250,
    "report_specification": 200,
    "cutting_mode": 100,
    "remarks": 500,
}
LABEL_OVERRIDE_FIELDS = tuple(LABEL_OVERRIDE_FIELD_LIMITS)
LABEL_OVERRIDE_SCHEMA_VERSION = 1


class MoldLabelContentError(ValueError):
    """Raised when label-only content cannot safely be stored or projected."""


def normalize_label_override_text(value: str | None, *, field: str) -> str | None:
    if field not in LABEL_OVERRIDE_FIELD_LIMITS:
        raise MoldLabelContentError("模具标签覆写字段无效")
    if value is None:
        return None
    if not isinstance(value, str):
        raise MoldLabelContentError("模具标签覆写内容必须是文本")
    normalized = " ".join(value.strip().split())
    if not normalized:
        return None
    if any(ord(character) < 32 for character in normalized):
        raise MoldLabelContentError("模具标签覆写内容不能包含控制字符")
    if len(normalized) > LABEL_OVERRIDE_FIELD_LIMITS[field]:
        raise MoldLabelContentError(
            f"模具标签覆写{field}不能超过{LABEL_OVERRIDE_FIELD_LIMITS[field]}个字符"
        )
    return normalized


def normalize_label_overrides(
    raw: Mapping[str, object] | None,
) -> dict[str, str | None]:
    if raw is None:
        return {field: None for field in LABEL_OVERRIDE_FIELDS}
    unknown = set(raw) - set(LABEL_OVERRIDE_FIELDS)
    if unknown:
        raise MoldLabelContentError("模具标签覆写包含未登记字段")
    return {
        field: normalize_label_override_text(raw.get(field), field=field)
        for field in LABEL_OVERRIDE_FIELDS
    }


def canonical_label_overrides(raw: Mapping[str, object] | None) -> str | None:
    normalized = normalize_label_overrides(raw)
    if not any(normalized.values()):
        return None
    return json.dumps(
        {"v": LABEL_OVERRIDE_SCHEMA_VERSION, **normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_label_overrides(value: str | None) -> dict[str, str | None]:
    if value is None or not str(value).strip():
        return {field: None for field in LABEL_OVERRIDE_FIELDS}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MoldLabelContentError("模具标签覆写记录损坏，请联系管理员核对") from error
    if not isinstance(parsed, dict) or parsed.get("v") != LABEL_OVERRIDE_SCHEMA_VERSION:
        raise MoldLabelContentError("模具标签覆写版本不受支持，请联系管理员核对")
    if set(parsed) - {"v", *LABEL_OVERRIDE_FIELDS}:
        raise MoldLabelContentError("模具标签覆写记录包含未登记字段，请联系管理员核对")
    return normalize_label_overrides(
        {field: parsed.get(field) for field in LABEL_OVERRIDE_FIELDS}
    )


def apply_label_overrides(
    auto_fields: Mapping[str, str], overrides: Mapping[str, str | None]
) -> dict[str, str]:
    return {
        field: str(overrides.get(field) or auto_fields.get(field) or "")
        for field in LABEL_OVERRIDE_FIELDS
    }


def mold_count_facts(product) -> list[dict]:
    """Only explicit component settings prove a mold's per-sheet die yield."""
    from app.services.sheet_cutting_settings import normalize_settings
    from app.services.sheet_cutting_contract import SheetCuttingContractError
    try:
        settings = normalize_settings(getattr(product, "sheet_cutting_settings", None))
    except SheetCuttingContractError:
        settings = None
    if settings is None:
        return [{"component_type": "whole", "mold_count": None, "display": "几模待核"}]
    labels = {"whole": "", "cover": "盖", "base": "底"}
    return [{"component_type": key, "mold_count": part["mold_count"],
             "display": f"{labels[key]}{part['mold_count']}模"}
            for key, part in settings.items() if key != "schema_version" and part["is_die_cut"]] or [
                {"component_type": "whole", "mold_count": None, "display": "几模待核"}]


def mold_count_projection(products) -> dict:
    facts = [{"product_id": product.id, "product_code": str(product.product_code or ""), **fact}
             for product in products for fact in mold_count_facts(product)]
    counts = {fact["mold_count"] for fact in facts}
    display = f"{next(iter(counts))}模" if len(counts) == 1 and None not in counts else "几模待核"
    return {"label_mold_count": display, "label_mold_count_facts": facts,
            "label_mold_count_consistent": bool(facts) and len(counts) == 1 and None not in counts}
