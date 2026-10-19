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


def canonical_label_overrides(raw: Mapping[str, object] | None, *, preserve_json: str | None = None) -> str | None:
    normalized = normalize_label_overrides(raw)
    titles = product_label_titles(preserve_json) if preserve_json else {}
    texts, scoped = label_text_settings(preserve_json)
    if not any(normalized.values()) and not titles and not texts and not scoped:
        return None
    return json.dumps(
        {"v": 3 if texts or scoped else 2 if titles else LABEL_OVERRIDE_SCHEMA_VERSION, **normalized, **({"product_titles": titles} if titles else {}), **({"text_overrides": texts, "product_text_overrides": scoped} if texts or scoped else {})},
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
    if not isinstance(parsed, dict) or parsed.get("v") not in (1, 2, 3):
        raise MoldLabelContentError("模具标签覆写版本不受支持，请联系管理员核对")
    if set(parsed) - {"v", "product_titles", "text_overrides", "product_text_overrides", *LABEL_OVERRIDE_FIELDS}:
        raise MoldLabelContentError("模具标签覆写记录包含未登记字段，请联系管理员核对")
    product_label_titles(value)
    label_text_settings(value)
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


def product_label_titles(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    raw = json.loads(value)
    titles = raw.get('product_titles', {})
    if not isinstance(titles, dict) or len(titles) > 200:
        raise MoldLabelContentError('模具产品标签简称记录无效')
    result = {}
    for key, text in titles.items():
        if not str(key).isdigit() or int(key) <= 0:
            raise MoldLabelContentError('标签简称产品身份无效')
        normalized = normalize_label_override_text(text, field='product_name')
        if normalized:
            result[str(key)] = normalized
    return result


def with_product_label_title(value: str | None, product_id: int, title: str) -> str:
    base = parse_label_overrides(value)
    titles = product_label_titles(value)
    normalized = normalize_label_override_text(title, field='product_name')
    if not normalized:
        raise MoldLabelContentError('标签简称不能为空')
    titles[str(product_id)] = normalized
    texts, scoped = label_text_settings(value)
    return json.dumps({'v': 3 if texts or scoped else 2, **base, 'product_titles': titles,
                      **({'text_overrides': texts, 'product_text_overrides': scoped} if texts or scoped else {})},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))


LABEL_TEXT_FIELDS = frozenset((
    'inventory_code', 'inventory_code_top', 'inventory_code_side', 'drawing_number', 'rack_location',
    'cutting_mode', 'customer_name', 'report_specification', 'custom_note',
    'product_name', 'customer_inventory_code', 'board_specification',
    'product_specification', 'flute_type', 'mold_identity', 'mold_chinese_short_name',
    'mold_label_name', 'mold_number',
))
LABEL_TEXT_READER = 'mold_label_text_v3'


def normalize_label_texts(raw):
    if not isinstance(raw, dict) or set(raw) - LABEL_TEXT_FIELDS:
        raise MoldLabelContentError('标签文字包含未登记字段')
    result = {}
    for key, value in raw.items():
        if not isinstance(value, str) or len(value) > 500:
            raise MoldLabelContentError('每项标签文字须为500字以内的文本')
        value = value.replace('\r\n', '\n').strip()
        if any(ord(c) < 32 and c != '\n' for c in value):
            raise MoldLabelContentError('标签文字不能包含控制字符')
        result[key] = value  # Empty means deliberately hidden, not automatic fallback.
    return result


def label_text_settings(value):
    raw = json.loads(value) if value else {}
    if not isinstance(raw, dict):
        raise MoldLabelContentError('标签文字资料无效')
    texts = normalize_label_texts(raw.get('text_overrides', {}))
    scoped = raw.get('product_text_overrides', {})
    if not isinstance(scoped, dict) or len(scoped) > 200:
        raise MoldLabelContentError('标签产品文字资料无效')
    result = {}
    for key, item in scoped.items():
        if not isinstance(key, str) or not key.isdigit() or int(key) <= 0:
            raise MoldLabelContentError('标签文字产品身份无效')
        result[key] = normalize_label_texts(item)
    return texts, result


def label_text_overrides(value, product_id=None):
    texts, scoped = label_text_settings(value)
    return {**texts, **scoped.get(str(product_id), {})} if product_id else texts


def with_label_texts(value, texts, product_id=None):
    base = parse_label_overrides(value)
    titles = product_label_titles(value)
    common, scoped = label_text_settings(value)
    texts = normalize_label_texts(texts)
    if product_id is None:
        common = texts
    elif texts:
        scoped[str(product_id)] = texts
    else:
        scoped.pop(str(product_id), None)
    return json.dumps({'v': 3 if common or scoped else 2 if titles else 1, **base, **({'product_titles': titles} if titles else {}),
                      **({'text_overrides': common, 'product_text_overrides': scoped} if common or scoped else {})},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
