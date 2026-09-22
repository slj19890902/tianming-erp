from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.delivery_print_template import DeliveryPrintTemplateRevision


CATALOG_VERSION = "delivery-print-v1"
ELEMENTS = (
    ("company_header", "公司抬头", 21.0),
    ("document_title", "单据标题", 18.0),
    ("customer_meta", "客户与单号", 12.0),
    ("item_table", "送货明细", 13.0),
    ("summary_notes", "合计与备注", 12.0),
    ("signature_footer", "签字与页脚", 11.0),
)
COLUMN_KEYS = (
    "sequence",
    "customer_po",
    "product_code",
    "product_name",
    "specification",
    "unit",
    "quantity",
    "remarks",
)
DEFAULT_COLUMN_WIDTHS = {
    "sequence": 4.0,
    "customer_po": 20.0,
    "product_code": 13.0,
    "product_name": 24.0,
    "specification": 16.0,
    "unit": 4.0,
    "quantity": 7.0,
    "remarks": 12.0,
}


class DeliveryPrintTemplateError(ValueError):
    pass


class DeliveryPrintTemplateConflict(DeliveryPrintTemplateError):
    pass


def default_layout() -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "elements": [
            {
                "id": element_id,
                "x_mm": 0.0,
                "y_mm": 0.0,
                "font_size_pt": font_size,
                "visible": True,
            }
            for element_id, _label, font_size in ELEMENTS
        ],
        "column_widths": dict(DEFAULT_COLUMN_WIDTHS),
        "show_remarks": True,
    }


def field_catalog() -> list[dict[str, Any]]:
    return [
        {"id": element_id, "label": label, "kind": "layout_group"}
        for element_id, label, _font_size in ELEMENTS
    ]


def _number(value: Any, *, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise DeliveryPrintTemplateError(f"{label}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise DeliveryPrintTemplateError(f"{label}必须是数字") from error
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise DeliveryPrintTemplateError(
            f"{label}必须在 {minimum:g} 到 {maximum:g} 之间"
        )
    return round(number, 2)


def normalize_layout(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("catalog_version") == "delivery-print-v2":
        from app.services.customer_delivery_templates import normalize_layout as normalize_v2
        try:
            return normalize_v2(value)
        except ValueError as error:
            raise DeliveryPrintTemplateError(str(error)) from error
    if not isinstance(value, dict) or value.get("catalog_version") != CATALOG_VERSION:
        raise DeliveryPrintTemplateError("送货模板字段目录版本无效")
    raw_elements = value.get("elements")
    if not isinstance(raw_elements, list):
        raise DeliveryPrintTemplateError("送货模板元素必须是列表")
    allowed = {item[0]: item for item in ELEMENTS}
    normalized_elements = []
    seen: set[str] = set()
    for raw in raw_elements:
        if not isinstance(raw, dict) or raw.get("id") not in allowed:
            raise DeliveryPrintTemplateError("送货模板含未登记字段")
        element_id = str(raw["id"])
        if element_id in seen:
            raise DeliveryPrintTemplateError("送货模板含重复字段")
        seen.add(element_id)
        visible = raw.get("visible", True)
        if not isinstance(visible, bool):
            raise DeliveryPrintTemplateError("字段显隐值无效")
        if not visible:
            raise DeliveryPrintTemplateError("送货单固定业务区域不能隐藏")
        normalized_elements.append(
            {
                "id": element_id,
                "x_mm": _number(
                    raw.get("x_mm", 0), label="横向位置", minimum=-12, maximum=12
                ),
                "y_mm": _number(
                    raw.get("y_mm", 0), label="纵向位置", minimum=-8, maximum=8
                ),
                "font_size_pt": _number(
                    raw.get("font_size_pt", allowed[element_id][2]),
                    label="字号",
                    minimum=7,
                    maximum=30,
                ),
                "visible": visible,
            }
        )
    if seen != set(allowed):
        raise DeliveryPrintTemplateError("送货模板缺少必需字段")
    raw_widths = value.get("column_widths")
    if not isinstance(raw_widths, dict) or set(raw_widths) != set(COLUMN_KEYS):
        raise DeliveryPrintTemplateError("送货模板明细列定义不完整")
    widths = {
        key: _number(raw_widths[key], label=f"{key}列宽", minimum=3, maximum=40)
        for key in COLUMN_KEYS
    }
    if not math.isclose(sum(widths.values()), 100.0, abs_tol=0.05):
        raise DeliveryPrintTemplateError("送货模板明细列宽合计必须为100%")
    show_remarks = value.get("show_remarks", True)
    if not isinstance(show_remarks, bool):
        raise DeliveryPrintTemplateError("备注显隐值无效")
    return {
        "catalog_version": CATALOG_VERSION,
        "elements": normalized_elements,
        "column_widths": widths,
        "show_remarks": show_remarks,
    }


def canonical_json(layout: dict[str, Any]) -> str:
    return json.dumps(
        normalize_layout(layout), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def profile_key(customer_id: int | None) -> str:
    return "default" if customer_id is None else f"customer:{int(customer_id)}"


def _latest(db: Session, key: str, stream: str) -> DeliveryPrintTemplateRevision | None:
    return db.scalar(
        select(DeliveryPrintTemplateRevision)
        .where(
            DeliveryPrintTemplateRevision.profile_key == key,
            DeliveryPrintTemplateRevision.stream == stream,
        )
        .order_by(desc(DeliveryPrintTemplateRevision.version))
        .limit(1)
    )


def _envelope(row: DeliveryPrintTemplateRevision | None, *, key: str) -> dict:
    if row is None:
        layout = default_layout()
        encoded = canonical_json(layout)
        return {
            "profile_key": key,
            "version": 0,
            "layout": layout,
            "layout_hash": payload_hash(encoded),
            "source": "built_in_default",
        }
    if payload_hash(row.payload_json) != row.payload_hash:
        raise DeliveryPrintTemplateError("送货模板版本哈希不一致")
    try:
        decoded = json.loads(row.payload_json)
    except json.JSONDecodeError as error:
        raise DeliveryPrintTemplateError("送货模板版本内容损坏") from error
    layout = normalize_layout(decoded)
    return {
        "profile_key": row.profile_key,
        "version": row.version,
        "layout": layout,
        "layout_hash": row.payload_hash,
        "source": "published" if row.stream == "release" else "draft",
    }


def effective_layout(db: Session, customer_id: int | None) -> dict:
    if customer_id is not None:
        customer = _latest(db, profile_key(customer_id), "release")
        if customer is not None:
            return _envelope(customer, key=customer.profile_key)
    default = _latest(db, "default", "release")
    return _envelope(default, key="default")


def snapshot_for_delivery(db: Session, customer_id: int) -> dict:
    selected = effective_layout(db, customer_id)
    encoded = canonical_json(selected["layout"])
    return {
        "profile_key": selected["profile_key"],
        "version": selected["version"],
        "payload_json": encoded,
        "payload_hash": payload_hash(encoded),
    }


def decode_snapshot(
    *, profile_key_value: str | None, version: int | None,
    payload_json: str | None, expected_hash: str | None,
) -> dict:
    if version is None:
        fallback = _envelope(None, key="legacy")
        fallback["source"] = "legacy_default_fallback"
        return fallback
    if not payload_json or not expected_hash or payload_hash(payload_json) != expected_hash:
        raise DeliveryPrintTemplateError("送货单冻结模板缺失或哈希不一致")
    try:
        decoded = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise DeliveryPrintTemplateError("送货单冻结模板内容损坏") from error
    return {
        "profile_key": profile_key_value or "default",
        "version": version,
        "layout": normalize_layout(decoded),
        "layout_hash": expected_hash,
        "source": "frozen",
    }


def admin_state(db: Session, customer_id: int | None) -> dict:
    from app.services.customer_delivery_templates import PRESETS, preset_layout
    key = profile_key(customer_id)
    released = _latest(db, key, "release")
    draft = _latest(db, key, "draft")
    published = _envelope(released, key=key)
    if customer_id is not None and released is None:
        inherited = _latest(db, "default", "release")
        if inherited is not None:
            published = {
                **_envelope(inherited, key="default"),
                "profile_key": key,
                "version": 0,
                "source": "inherited_default",
                "inherited_release_version": inherited.version,
            }
    return {
        "profile_key": key,
        "customer_id": customer_id,
        "catalog": field_catalog(),
        "presets": PRESETS,
        "legacy_layout": default_layout(),
        "preset_layouts": {key: preset_layout(key) for key in PRESETS},
        "published": published,
        "draft": (
            _envelope(draft, key=key)
            if draft and draft.base_release_version == (released.version if released else 0)
            else None
        ),
    }


def _request_hash(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _replay(db: Session, operation_key: str, request_hash: str) -> dict | None:
    row = db.scalar(
        select(DeliveryPrintTemplateRevision).where(
            DeliveryPrintTemplateRevision.operation_key == operation_key
        )
    )
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise DeliveryPrintTemplateConflict("同一操作键不能更换模板内容")
    result = _envelope(row, key=row.profile_key)
    result["replayed"] = True
    return result


def save_draft(
    db: Session, *, customer_id: int | None, layout: dict[str, Any],
    expected_release_version: int, operation_key: str, actor_id: int,
) -> dict:
    key = profile_key(customer_id)
    normalized = normalize_layout(layout)
    if customer_id is None and normalized["catalog_version"] == "delivery-print-v2":
        raise DeliveryPrintTemplateError("客户专用版式必须选择客户")
    request = _request_hash({"kind": "save_draft", "key": key, "layout": normalized,
                             "expected": expected_release_version})
    replay = _replay(db, operation_key, request)
    if replay:
        return replay
    current = _latest(db, key, "release")
    current_version = current.version if current else 0
    if current_version != expected_release_version:
        raise DeliveryPrintTemplateConflict("已发布模板已变化，请重新加载")
    previous = _latest(db, key, "draft")
    encoded = canonical_json(normalized)
    row = DeliveryPrintTemplateRevision(
        profile_key=key, customer_id=customer_id, stream="draft",
        version=(previous.version + 1 if previous else 1),
        catalog_version=normalized['catalog_version'], payload_json=encoded,
        payload_hash=payload_hash(encoded), base_release_version=current_version,
        operation_kind="save_draft", operation_key=operation_key,
        request_hash=request, created_by=actor_id,
    )
    db.add(row)
    db.flush()
    result = _envelope(row, key=key)
    result["replayed"] = False
    return result


def publish_draft(
    db: Session, *, customer_id: int | None, draft_version: int,
    expected_release_version: int, operation_key: str, actor_id: int,
) -> dict:
    key = profile_key(customer_id)
    request = _request_hash({"kind": "publish", "key": key, "draft": draft_version,
                             "expected": expected_release_version})
    replay = _replay(db, operation_key, request)
    if replay:
        return replay
    current = _latest(db, key, "release")
    current_version = current.version if current else 0
    if current_version != expected_release_version:
        raise DeliveryPrintTemplateConflict("已发布模板已变化，请重新加载")
    draft = db.scalar(select(DeliveryPrintTemplateRevision).where(
        DeliveryPrintTemplateRevision.profile_key == key,
        DeliveryPrintTemplateRevision.stream == "draft",
        DeliveryPrintTemplateRevision.version == draft_version,
    ))
    if draft is None or draft.base_release_version != current_version:
        raise DeliveryPrintTemplateConflict("草稿已过期，请重新保存")
    row = DeliveryPrintTemplateRevision(
        profile_key=key, customer_id=customer_id, stream="release",
        version=current_version + 1, catalog_version=draft.catalog_version,
        payload_json=draft.payload_json, payload_hash=draft.payload_hash,
        base_release_version=current_version, operation_kind="publish",
        operation_key=operation_key, request_hash=request, created_by=actor_id,
    )
    db.add(row)
    db.flush()
    result = _envelope(row, key=key)
    result["replayed"] = False
    return result


def rollback_release(
    db: Session, *, customer_id: int | None, source_version: int,
    expected_release_version: int, operation_key: str, actor_id: int,
) -> dict:
    key = profile_key(customer_id)
    request = _request_hash({"kind": "rollback", "key": key, "source": source_version,
                             "expected": expected_release_version})
    replay = _replay(db, operation_key, request)
    if replay:
        return replay
    current = _latest(db, key, "release")
    current_version = current.version if current else 0
    if current_version != expected_release_version:
        raise DeliveryPrintTemplateConflict("已发布模板已变化，请重新加载")
    source = db.scalar(select(DeliveryPrintTemplateRevision).where(
        DeliveryPrintTemplateRevision.profile_key == key,
        DeliveryPrintTemplateRevision.stream == "release",
        DeliveryPrintTemplateRevision.version == source_version,
    ))
    if source is None:
        raise DeliveryPrintTemplateError("回滚目标版本不存在")
    row = DeliveryPrintTemplateRevision(
        profile_key=key, customer_id=customer_id, stream="release",
        version=current_version + 1, catalog_version=source.catalog_version,
        payload_json=source.payload_json, payload_hash=source.payload_hash,
        base_release_version=current_version, operation_kind="rollback",
        operation_key=operation_key, request_hash=request,
        source_release_version=source_version, created_by=actor_id,
    )
    db.add(row)
    db.flush()
    result = _envelope(row, key=key)
    result["replayed"] = False
    return result
