from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.production_label_print import ProductionPackagingLabelLayoutRevision


CATALOG_VERSION = "p1-66b-v1"
PAPER_WIDTH_MM = 40.0
PAPER_HEIGHT_MM = 30.0
DEFAULT_QUANTITY_FIXED_SUFFIX = "只/捆"
MAX_FIXED_SUFFIX_LENGTH = 8
OVERLAP_EPSILON_MM = 0.001
_SAFE_FIXED_SUFFIX_PATTERN = re.compile(r"^[A-Za-z\u3400-\u9fff/／·_ -]*$")
_DANGEROUS_FIXED_SUFFIX_PATTERN = re.compile(
    r"(?:script|javascript|alert|onerror|onload|style|data|https?|www|url)",
    re.IGNORECASE,
)
ELEMENT_CATALOG = (
    {
        "id": "customer_short_name",
        "label": "客户中文简称",
        "kind": "text",
        "required": True,
        "hideable": True,
    },
    {
        "id": "product_code",
        "label": "存货编码",
        "kind": "text",
        "required": True,
        "hideable": True,
    },
    {
        "id": "product_name",
        "label": "产品名称",
        "kind": "text",
        "required": True,
        "hideable": True,
    },
    {
        "id": "specification",
        "label": "规格",
        "kind": "text",
        "required": True,
        "hideable": True,
    },
    {
        "id": "quantity",
        "label": "数量（只/捆）",
        "kind": "text",
        "required": True,
        "hideable": True,
        "fixed_suffix_editable": True,
        "default_fixed_suffix": DEFAULT_QUANTITY_FIXED_SUFFIX,
    },
    {
        "id": "product_qr",
        "label": "产品二维码（等待 P1-64C 数据源）",
        "kind": "qr",
        "required": False,
        "source_available": False,
    },
)
_CATALOG_BY_ID = {item["id"]: item for item in ELEMENT_CATALOG}
_BASE_ELEMENT_KEYS = {
    "id",
    "kind",
    "x_mm",
    "y_mm",
    "width_mm",
    "height_mm",
    "visible",
}
_TEXT_ELEMENT_KEYS = _BASE_ELEMENT_KEYS | {
    "font_size_mm",
    "font_weight",
    "text_align",
}


class ProductionPackagingLabelLayoutError(ValueError):
    pass


class ProductionPackagingLabelLayoutConflict(
    ProductionPackagingLabelLayoutError
):
    pass


def default_layout() -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "paper": {"width_mm": PAPER_WIDTH_MM, "height_mm": PAPER_HEIGHT_MM},
        "elements": [
            {
                "id": "customer_short_name",
                "kind": "text",
                "x_mm": 0.8,
                "y_mm": 0.6,
                "width_mm": 38.4,
                "height_mm": 4.2,
                "font_size_mm": 3.65,
                "font_weight": 900,
                "text_align": "center",
                "visible": True,
            },
            {
                "id": "product_code",
                "kind": "text",
                "x_mm": 0.8,
                "y_mm": 5.0,
                "width_mm": 38.4,
                "height_mm": 5.9,
                "font_size_mm": 3.6,
                "font_weight": 900,
                "text_align": "center",
                "visible": True,
            },
            {
                "id": "product_name",
                "kind": "text",
                "x_mm": 0.8,
                "y_mm": 11.2,
                "width_mm": 38.4,
                "height_mm": 4.2,
                "font_size_mm": 2.85,
                "font_weight": 800,
                "text_align": "center",
                "visible": True,
            },
            {
                "id": "specification",
                "kind": "text",
                "x_mm": 0.8,
                "y_mm": 15.6,
                "width_mm": 38.4,
                "height_mm": 4.2,
                "font_size_mm": 2.85,
                "font_weight": 800,
                "text_align": "center",
                "visible": True,
            },
            {
                "id": "quantity",
                "kind": "text",
                "x_mm": 0.8,
                "y_mm": 20.2,
                "width_mm": 38.4,
                "height_mm": 9.0,
                "font_size_mm": 6.1,
                "font_weight": 900,
                "text_align": "center",
                "visible": True,
            },
            {
                "id": "product_qr",
                "kind": "qr",
                "x_mm": 28.0,
                "y_mm": 18.0,
                "width_mm": 11.0,
                "height_mm": 11.0,
                "visible": False,
            },
        ],
    }


def catalog() -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "paper": {"width_mm": PAPER_WIDTH_MM, "height_mm": PAPER_HEIGHT_MM},
        "elements": [dict(item) for item in ELEMENT_CATALOG],
    }


def _finite_number(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ProductionPackagingLabelLayoutError(f"{name} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ProductionPackagingLabelLayoutError(f"{name} 必须是数字") from error
    if not math.isfinite(number):
        raise ProductionPackagingLabelLayoutError(f"{name} 必须是有限数字")
    return round(number, 3)


def _normalize_fixed_suffix(value: object) -> str:
    if not isinstance(value, str):
        raise ProductionPackagingLabelLayoutError("数量后的固定说明必须是文字")
    normalized = value.strip()
    if len(normalized) > MAX_FIXED_SUFFIX_LENGTH:
        raise ProductionPackagingLabelLayoutError(
            f"数量后的固定说明最多{MAX_FIXED_SUFFIX_LENGTH}个字符"
        )
    if (
        not _SAFE_FIXED_SUFFIX_PATTERN.fullmatch(normalized)
        or _DANGEROUS_FIXED_SUFFIX_PATTERN.search(normalized)
    ):
        raise ProductionPackagingLabelLayoutError(
            "数量后的固定说明只能使用简短中文、英文字母或单位分隔符"
        )
    return normalized


def _validate_visible_element_overlaps(elements: list[dict[str, Any]]) -> None:
    visible = [element for element in elements if element["visible"]]
    for index, first in enumerate(visible):
        for second in visible[index + 1 :]:
            overlaps = (
                first["x_mm"]
                < second["x_mm"] + second["width_mm"] - OVERLAP_EPSILON_MM
                and second["x_mm"]
                < first["x_mm"] + first["width_mm"] - OVERLAP_EPSILON_MM
                and first["y_mm"]
                < second["y_mm"] + second["height_mm"] - OVERLAP_EPSILON_MM
                and second["y_mm"]
                < first["y_mm"] + first["height_mm"] - OVERLAP_EPSILON_MM
            )
            if overlaps:
                raise ProductionPackagingLabelLayoutError(
                    f"{_CATALOG_BY_ID[first['id']]['label']}与"
                    f"{_CATALOG_BY_ID[second['id']]['label']}不能重叠"
                )


def _normalize_element(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProductionPackagingLabelLayoutError("布局中存在无效元素")
    element_id = str(raw.get("id") or "").strip()
    metadata = _CATALOG_BY_ID.get(element_id)
    if metadata is None:
        raise ProductionPackagingLabelLayoutError("布局中存在未登记元素")
    if raw.get("kind") != metadata["kind"]:
        raise ProductionPackagingLabelLayoutError(f"{element_id} 的元素类型无效")
    if "fixed_suffix" in raw and element_id != "quantity":
        raise ProductionPackagingLabelLayoutError("只有数量元素可以设置固定说明")
    allowed_keys = (
        _TEXT_ELEMENT_KEYS | ({"fixed_suffix"} if element_id == "quantity" else set())
        if metadata["kind"] == "text"
        else _BASE_ELEMENT_KEYS
    )
    if set(raw) - allowed_keys:
        raise ProductionPackagingLabelLayoutError(
            f"{metadata['label']}包含未登记的布局字段"
        )
    visible = raw.get("visible")
    if not isinstance(visible, bool):
        raise ProductionPackagingLabelLayoutError(f"{element_id} 的显示状态无效")
    if metadata["kind"] == "qr" and visible and not metadata.get(
        "source_available"
    ):
        raise ProductionPackagingLabelLayoutError(
            "产品二维码数据源尚未启用，当前只能预留位置，不能正式显示"
        )
    normalized = {
        "id": element_id,
        "kind": metadata["kind"],
        "x_mm": _finite_number(raw.get("x_mm"), name=f"{metadata['label']} X"),
        "y_mm": _finite_number(raw.get("y_mm"), name=f"{metadata['label']} Y"),
        "width_mm": _finite_number(
            raw.get("width_mm"), name=f"{metadata['label']}宽度"
        ),
        "height_mm": _finite_number(
            raw.get("height_mm"), name=f"{metadata['label']}高度"
        ),
        "visible": visible,
    }
    if normalized["x_mm"] < 0 or normalized["y_mm"] < 0:
        raise ProductionPackagingLabelLayoutError(f"{metadata['label']} 不能移出纸张")
    if normalized["width_mm"] <= 0 or normalized["height_mm"] <= 0:
        raise ProductionPackagingLabelLayoutError(f"{metadata['label']} 宽高必须大于0")
    if (
        normalized["x_mm"] + normalized["width_mm"] > PAPER_WIDTH_MM + 0.001
        or normalized["y_mm"] + normalized["height_mm"]
        > PAPER_HEIGHT_MM + 0.001
    ):
        raise ProductionPackagingLabelLayoutError(f"{metadata['label']} 超出40×30纸张")
    if metadata["kind"] == "text":
        font_size = _finite_number(
            raw.get("font_size_mm"), name=f"{metadata['label']}字号"
        )
        if not 1.0 <= font_size <= 10.0:
            raise ProductionPackagingLabelLayoutError(
                f"{metadata['label']}字号必须在1至10毫米之间"
            )
        font_weight = raw.get("font_weight")
        if isinstance(font_weight, bool) or font_weight not in {400, 700, 800, 900}:
            raise ProductionPackagingLabelLayoutError(
                f"{metadata['label']}字重无效"
            )
        text_align = str(raw.get("text_align") or "")
        if text_align not in {"left", "center", "right"}:
            raise ProductionPackagingLabelLayoutError(
                f"{metadata['label']}对齐方式无效"
            )
        normalized.update(
            {
                "font_size_mm": font_size,
                "font_weight": int(font_weight),
                "text_align": text_align,
            }
        )
        if "fixed_suffix" in raw:
            normalized["fixed_suffix"] = _normalize_fixed_suffix(
                raw.get("fixed_suffix")
            )
    return normalized


def normalize_layout(
    payload: object,
    *,
    validate_visible_overlaps: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ProductionPackagingLabelLayoutError("标签布局必须是对象")
    if set(payload) - {"catalog_version", "paper", "elements"}:
        raise ProductionPackagingLabelLayoutError("标签布局包含未登记字段")
    if payload.get("catalog_version") != CATALOG_VERSION:
        raise ProductionPackagingLabelLayoutError(
            "标签元素目录版本已变化，请重新加载默认布局"
        )
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise ProductionPackagingLabelLayoutError("标签纸张定义无效")
    if set(paper) - {"width_mm", "height_mm"}:
        raise ProductionPackagingLabelLayoutError("标签纸张包含未登记字段")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != PAPER_HEIGHT_MM
    ):
        raise ProductionPackagingLabelLayoutError("本任务只允许40×30毫米纸张")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise ProductionPackagingLabelLayoutError("标签元素必须是列表")
    normalized_elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_elements:
        normalized = _normalize_element(raw)
        if normalized["id"] in seen:
            raise ProductionPackagingLabelLayoutError(
                f"布局中存在重复元素 {normalized['id']}"
            )
        seen.add(normalized["id"])
        normalized_elements.append(normalized)
    missing = [item["id"] for item in ELEMENT_CATALOG if item["id"] not in seen]
    if missing:
        raise ProductionPackagingLabelLayoutError(
            f"布局缺少已登记元素：{','.join(missing)}"
        )
    order = {item["id"]: index for index, item in enumerate(ELEMENT_CATALOG)}
    normalized_elements.sort(key=lambda item: order[item["id"]])
    if validate_visible_overlaps:
        _validate_visible_element_overlaps(normalized_elements)
    return {
        "catalog_version": CATALOG_VERSION,
        "paper": {"width_mm": PAPER_WIDTH_MM, "height_mm": PAPER_HEIGHT_MM},
        "elements": normalized_elements,
    }


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def layout_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _latest(
    db: Session, stream: str
) -> ProductionPackagingLabelLayoutRevision | None:
    return db.scalar(
        select(ProductionPackagingLabelLayoutRevision)
        .where(ProductionPackagingLabelLayoutRevision.stream == stream)
        .order_by(desc(ProductionPackagingLabelLayoutRevision.version))
        .limit(1)
    )


def _version(row: ProductionPackagingLabelLayoutRevision | None) -> int:
    return int(row.version) if row is not None else 0


def _row_layout(row: ProductionPackagingLabelLayoutRevision) -> dict[str, Any]:
    if row.catalog_version != CATALOG_VERSION:
        raise ProductionPackagingLabelLayoutError("保存的标签元素目录版本已过期")
    try:
        raw = json.loads(row.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProductionPackagingLabelLayoutError("保存的标签布局已损坏") from error
    normalized = normalize_layout(raw, validate_visible_overlaps=False)
    if layout_hash(normalized) != row.payload_hash:
        raise ProductionPackagingLabelLayoutError("保存的标签布局校验失败")
    return normalized


def _envelope(
    row: ProductionPackagingLabelLayoutRevision | None,
    *,
    layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective = layout if layout is not None else (
        _row_layout(row) if row is not None else default_layout()
    )
    return {
        "version": _version(row),
        "layout": effective,
        "layout_hash": layout_hash(effective),
    }


def effective_layout(db: Session) -> dict[str, Any]:
    return _envelope(_latest(db, "release"))


def _history(
    db: Session, limit: int = 10
) -> list[ProductionPackagingLabelLayoutRevision]:
    return list(
        db.scalars(
            select(ProductionPackagingLabelLayoutRevision)
            .where(ProductionPackagingLabelLayoutRevision.stream == "release")
            .order_by(desc(ProductionPackagingLabelLayoutRevision.version))
            .limit(limit)
        ).all()
    )


def admin_state(db: Session) -> dict[str, Any]:
    draft = _latest(db, "draft")
    release = _latest(db, "release")
    release_envelope = _envelope(release)
    draft_envelope = _envelope(
        draft,
        layout=_row_layout(draft) if draft is not None else release_envelope["layout"],
    )
    history = _history(db)
    return {
        "catalog": catalog(),
        "draft": {
            **draft_envelope,
            "base_release_version": (
                int(draft.base_release_version)
                if draft is not None
                else release_envelope["version"]
            ),
        },
        "published": release_envelope,
        "history": [
            {
                "version": int(row.version),
                "operation_kind": row.operation_kind,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in history
        ],
        "can_rollback": len(history) >= 2,
    }


def _request_hash(operation_kind: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json({"operation_kind": operation_kind, **payload}).encode(
            "utf-8"
        )
    ).hexdigest()


def _operation_replay(
    db: Session, operation_key: str, request_hash: str
) -> ProductionPackagingLabelLayoutRevision | None:
    row = db.scalar(
        select(ProductionPackagingLabelLayoutRevision).where(
            ProductionPackagingLabelLayoutRevision.operation_key == operation_key
        )
    )
    if row is not None and row.request_hash != request_hash:
        raise ProductionPackagingLabelLayoutConflict(
            "操作编号已用于不同的标签布局请求"
        )
    return row


def _append_revision(
    db: Session,
    *,
    stream: str,
    version: int,
    layout: dict[str, Any],
    base_release_version: int,
    operation_kind: str,
    actor_id: int | None,
    operation_key: str | None = None,
    request_hash: str | None = None,
    source_release_version: int | None = None,
) -> ProductionPackagingLabelLayoutRevision:
    normalized = normalize_layout(layout)
    row = ProductionPackagingLabelLayoutRevision(
        stream=stream,
        version=version,
        catalog_version=CATALOG_VERSION,
        payload_json=_canonical_json(normalized),
        payload_hash=layout_hash(normalized),
        base_release_version=base_release_version,
        operation_kind=operation_kind,
        operation_key=operation_key,
        request_hash=request_hash,
        source_release_version=source_release_version,
        created_by=actor_id,
    )
    db.add(row)
    db.flush()
    return row


def _result(db: Session, *, replayed: bool, operation_kind: str) -> dict[str, Any]:
    return {
        **admin_state(db),
        "replayed": replayed,
        "operation_kind": operation_kind,
    }


def save_draft(
    db: Session,
    *,
    layout: dict[str, Any],
    expected_draft_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    normalized = normalize_layout(layout)
    request = {
        "layout": normalized,
        "expected_draft_version": expected_draft_version,
    }
    request_hash = _request_hash("save_draft", request)
    if _operation_replay(db, operation_key, request_hash):
        return _result(db, replayed=True, operation_kind="save_draft")
    draft = _latest(db, "draft")
    release = _latest(db, "release")
    if expected_draft_version != _version(draft):
        raise ProductionPackagingLabelLayoutConflict(
            "标签布局草稿版本已变化，请重新加载"
        )
    _append_revision(
        db,
        stream="draft",
        version=_version(draft) + 1,
        layout=normalized,
        base_release_version=_version(release),
        operation_kind="save_draft",
        operation_key=operation_key,
        request_hash=request_hash,
        actor_id=actor_id,
    )
    return _result(db, replayed=False, operation_kind="save_draft")


def publish_draft(
    db: Session,
    *,
    expected_draft_version: int,
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    request = {
        "expected_draft_version": expected_draft_version,
        "expected_release_version": expected_release_version,
    }
    request_hash = _request_hash("publish", request)
    if _operation_replay(db, operation_key, request_hash):
        return _result(db, replayed=True, operation_kind="publish")
    draft = _latest(db, "draft")
    release = _latest(db, "release")
    if draft is None:
        raise ProductionPackagingLabelLayoutConflict("请先保存标签布局草稿")
    if (
        expected_draft_version != _version(draft)
        or expected_release_version != _version(release)
    ):
        raise ProductionPackagingLabelLayoutConflict(
            "标签草稿或已发布版本已变化，请重新加载"
        )
    if int(draft.base_release_version) != _version(release):
        raise ProductionPackagingLabelLayoutConflict(
            "草稿基于旧发布版本，请重新加载后再保存"
        )
    layout = _row_layout(draft)
    release_version = _version(release) + 1
    _append_revision(
        db,
        stream="release",
        version=release_version,
        layout=layout,
        base_release_version=_version(release),
        operation_kind="publish",
        operation_key=operation_key,
        request_hash=request_hash,
        actor_id=actor_id,
    )
    _append_revision(
        db,
        stream="draft",
        version=_version(draft) + 1,
        layout=layout,
        base_release_version=release_version,
        operation_kind="publish_sync",
        actor_id=actor_id,
    )
    return _result(db, replayed=False, operation_kind="publish")


def _replace_release(
    db: Session,
    *,
    expected_draft_version: int,
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
    operation_kind: str,
    replacement: dict[str, Any],
    source_release_version: int | None,
) -> dict[str, Any]:
    request = {
        "expected_draft_version": expected_draft_version,
        "expected_release_version": expected_release_version,
    }
    request_hash = _request_hash(operation_kind, request)
    if _operation_replay(db, operation_key, request_hash):
        return _result(db, replayed=True, operation_kind=operation_kind)
    draft = _latest(db, "draft")
    release = _latest(db, "release")
    if (
        expected_draft_version != _version(draft)
        or expected_release_version != _version(release)
    ):
        raise ProductionPackagingLabelLayoutConflict(
            "标签草稿或已发布版本已变化，请重新加载"
        )
    normalized = normalize_layout(replacement)
    release_version = _version(release) + 1
    _append_revision(
        db,
        stream="release",
        version=release_version,
        layout=normalized,
        base_release_version=_version(release),
        operation_kind=operation_kind,
        operation_key=operation_key,
        request_hash=request_hash,
        source_release_version=source_release_version,
        actor_id=actor_id,
    )
    _append_revision(
        db,
        stream="draft",
        version=_version(draft) + 1,
        layout=normalized,
        base_release_version=release_version,
        operation_kind=f"{operation_kind}_sync",
        source_release_version=source_release_version,
        actor_id=actor_id,
    )
    return _result(db, replayed=False, operation_kind=operation_kind)


def restore_default(
    db: Session,
    **kwargs: Any,
) -> dict[str, Any]:
    return _replace_release(
        db,
        **kwargs,
        operation_kind="restore_default",
        replacement=default_layout(),
        source_release_version=None,
    )


def rollback_release(
    db: Session,
    **kwargs: Any,
) -> dict[str, Any]:
    history = _history(db, limit=2)
    if len(history) < 2:
        raise ProductionPackagingLabelLayoutConflict(
            "当前没有上一版已发布标签布局可回滚"
        )
    source = history[1]
    return _replace_release(
        db,
        **kwargs,
        operation_kind="rollback",
        replacement=_row_layout(source),
        source_release_version=int(source.version),
    )


def layout_diff_summary(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    old = {item["id"]: item for item in before["elements"]}
    new = {item["id"]: item for item in after["elements"]}
    changed = [element_id for element_id, item in new.items() if old.get(element_id) != item]
    return {"changed_count": len(changed), "changed_ids": changed}
