from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.mold_tool import MoldLabelLayoutRevision


V1_CATALOG_VERSION = "p1-103-v1"
V1_PAPER_WIDTH_MM = 80.0
V1_PAPER_HEIGHT_MM = 40.0
V1_ELEMENT_CATALOG = (
    {"id": "board_specification", "label": "片料尺寸", "kind": "text"},
    {"id": "inventory_code", "label": "纸箱存货编码", "kind": "text"},
    {"id": "flute_type", "label": "楞型", "kind": "text"},
    {"id": "cutting_mode", "label": "开料方式", "kind": "text"},
    {"id": "customer_name", "label": "客户名称", "kind": "text"},
    {"id": "mold_label_name", "label": "模具标签名称", "kind": "text"},
    {
        "id": "mold_chinese_short_name",
        "label": "模具中文简写",
        "kind": "text",
    },
    {"id": "product_specification", "label": "产品尺寸", "kind": "text"},
    {"id": "mold_qr", "label": "模具二维码", "kind": "qr"},
)
_V1_CATALOG_BY_ID = {item["id"]: item for item in V1_ELEMENT_CATALOG}

# These aliases describe the catalog accepted for new writes.  Historical
# print snapshots use their own version-pinned decoder below.
CATALOG_VERSION = V1_CATALOG_VERSION
PAPER_WIDTH_MM = V1_PAPER_WIDTH_MM
PAPER_HEIGHT_MM = V1_PAPER_HEIGHT_MM
ELEMENT_CATALOG = V1_ELEMENT_CATALOG


class MoldLabelLayoutError(ValueError):
    pass


class MoldLabelLayoutConflict(MoldLabelLayoutError):
    pass


def default_layout() -> dict[str, Any]:
    """Return the verified 80 mm long-edge layout in millimetres.

    The lower 14.4 mm band is the mold-side identity strip: its two text rows
    are aligned with the QR code so the facts used while finding a mold stay
    visible on the approximately 15 mm mold edge.
    """

    return {
        "catalog_version": CATALOG_VERSION,
        "paper": {"width_mm": PAPER_WIDTH_MM, "height_mm": PAPER_HEIGHT_MM},
        "elements": [
            {
                "id": "board_specification",
                "kind": "text",
                "x_mm": 1.5,
                "y_mm": 1.2,
                "width_mm": 61.5,
                "height_mm": 8.6,
                "font_size_mm": 6.0,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "inventory_code",
                "kind": "text",
                "x_mm": 1.5,
                "y_mm": 10.5,
                "width_mm": 38.0,
                "height_mm": 12.0,
                "font_size_mm": 4.6,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "flute_type",
                "kind": "text",
                "x_mm": 40.3,
                "y_mm": 10.5,
                "width_mm": 22.7,
                "height_mm": 5.5,
                "font_size_mm": 3.8,
                "font_weight": 800,
                "text_align": "center",
                "visible": True,
            },
            {
                "id": "cutting_mode",
                "kind": "text",
                "x_mm": 40.3,
                "y_mm": 17.0,
                "width_mm": 22.7,
                "height_mm": 5.5,
                "font_size_mm": 3.4,
                "font_weight": 800,
                "text_align": "right",
                "visible": True,
            },
            {
                "id": "customer_name",
                "kind": "text",
                "x_mm": 1.5,
                "y_mm": 24.2,
                "width_mm": 14.0,
                "height_mm": 6.4,
                "font_size_mm": 4.0,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_label_name",
                "kind": "text",
                "x_mm": 16.1,
                "y_mm": 24.2,
                "width_mm": 46.9,
                "height_mm": 6.4,
                "font_size_mm": 5.0,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_chinese_short_name",
                "kind": "text",
                "x_mm": 1.5,
                "y_mm": 31.2,
                "width_mm": 17.5,
                "height_mm": 7.0,
                "font_size_mm": 4.0,
                "font_weight": 800,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "product_specification",
                "kind": "text",
                "x_mm": 19.7,
                "y_mm": 31.2,
                "width_mm": 43.3,
                "height_mm": 7.0,
                "font_size_mm": 4.5,
                "font_weight": 800,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_qr",
                "kind": "qr",
                "x_mm": 64.3,
                "y_mm": 24.6,
                "width_mm": 14.2,
                "height_mm": 14.2,
                "visible": True,
            },
        ],
    }


def catalog() -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "paper": {"width_mm": PAPER_WIDTH_MM, "height_mm": PAPER_HEIGHT_MM},
        "elements": [dict(item, required=True) for item in ELEMENT_CATALOG],
    }


def _finite_number(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise MoldLabelLayoutError(f"{name}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise MoldLabelLayoutError(f"{name}必须是数字") from error
    if not math.isfinite(number):
        raise MoldLabelLayoutError(f"{name}必须是有限数字")
    return round(number, 3)


def _normalize_element_v1(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MoldLabelLayoutError("布局中存在无效元素")
    element_id = str(raw.get("id") or "").strip()
    metadata = _V1_CATALOG_BY_ID.get(element_id)
    if metadata is None:
        raise MoldLabelLayoutError("布局中存在未登记元素")
    if raw.get("kind") != metadata["kind"]:
        raise MoldLabelLayoutError(f"{metadata['label']}的元素类型无效")
    if raw.get("visible") is not True:
        raise MoldLabelLayoutError(f"{metadata['label']}不能隐藏")
    normalized = {
        "id": element_id,
        "kind": metadata["kind"],
        "x_mm": _finite_number(raw.get("x_mm"), name=f"{metadata['label']} X位置"),
        "y_mm": _finite_number(raw.get("y_mm"), name=f"{metadata['label']} Y位置"),
        "width_mm": _finite_number(
            raw.get("width_mm"), name=f"{metadata['label']}宽度"
        ),
        "height_mm": _finite_number(
            raw.get("height_mm"), name=f"{metadata['label']}高度"
        ),
        "visible": True,
    }
    if normalized["x_mm"] < 0 or normalized["y_mm"] < 0:
        raise MoldLabelLayoutError(f"{metadata['label']}不能移出纸张")
    if normalized["width_mm"] <= 0 or normalized["height_mm"] <= 0:
        raise MoldLabelLayoutError(f"{metadata['label']}宽高必须大于0")
    if (
        normalized["x_mm"] + normalized["width_mm"] > V1_PAPER_WIDTH_MM + 0.001
        or normalized["y_mm"] + normalized["height_mm"]
        > V1_PAPER_HEIGHT_MM + 0.001
    ):
        raise MoldLabelLayoutError(f"{metadata['label']}超出80×40标签内容区")
    if metadata["kind"] == "qr":
        if abs(normalized["width_mm"] - normalized["height_mm"]) > 0.001:
            raise MoldLabelLayoutError("模具二维码必须保持正方形")
        if normalized["width_mm"] != 14.2:
            raise MoldLabelLayoutError("模具二维码必须保持已验证的14.2毫米边长")
    else:
        font_size = _finite_number(
            raw.get("font_size_mm"), name=f"{metadata['label']}字号"
        )
        if not 1.2 <= font_size <= 8.0:
            raise MoldLabelLayoutError(
                f"{metadata['label']}字号必须在1.2至8毫米之间"
            )
        font_weight = raw.get("font_weight")
        if isinstance(font_weight, bool) or font_weight not in {400, 700, 800, 900}:
            raise MoldLabelLayoutError(f"{metadata['label']}字重无效")
        text_align = str(raw.get("text_align") or "")
        if text_align not in {"left", "center", "right"}:
            raise MoldLabelLayoutError(f"{metadata['label']}对齐方式无效")
        normalized.update(
            {
                "font_size_mm": font_size,
                "font_weight": int(font_weight),
                "text_align": text_align,
            }
        )
    return normalized


def _rectangles_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    tolerance = 0.04
    return not (
        left["x_mm"] + left["width_mm"] <= right["x_mm"] + tolerance
        or right["x_mm"] + right["width_mm"] <= left["x_mm"] + tolerance
        or left["y_mm"] + left["height_mm"] <= right["y_mm"] + tolerance
        or right["y_mm"] + right["height_mm"] <= left["y_mm"] + tolerance
    )


def _normalize_layout_v1(payload: object) -> dict[str, Any]:
    """Decode the immutable P1-103 v1 catalog.

    Keep this decoder unchanged when a later catalog is introduced; register a
    new decoder instead so frozen historical print jobs remain replayable.
    """

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != V1_CATALOG_VERSION:
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise MoldLabelLayoutError("标签纸张定义无效")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != V1_PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != V1_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError("本布局只允许80×40毫米内容区")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise MoldLabelLayoutError("标签元素必须是列表")
    normalized_elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_elements:
        normalized = _normalize_element_v1(raw)
        if normalized["id"] in seen:
            raise MoldLabelLayoutError(f"布局中存在重复元素 {normalized['id']}")
        seen.add(normalized["id"])
        normalized_elements.append(normalized)
    missing = [
        item["id"] for item in V1_ELEMENT_CATALOG if item["id"] not in seen
    ]
    if missing:
        raise MoldLabelLayoutError(f"布局缺少已登记元素：{','.join(missing)}")
    for index, left in enumerate(normalized_elements):
        for right in normalized_elements[index + 1 :]:
            if _rectangles_overlap(left, right):
                raise MoldLabelLayoutError(
                    f"{_V1_CATALOG_BY_ID[left['id']]['label']}与"
                    f"{_V1_CATALOG_BY_ID[right['id']]['label']}发生重叠"
                )
    order = {
        item["id"]: index for index, item in enumerate(V1_ELEMENT_CATALOG)
    }
    normalized_elements.sort(key=lambda item: order[item["id"]])
    return {
        "catalog_version": V1_CATALOG_VERSION,
        "paper": {
            "width_mm": V1_PAPER_WIDTH_MM,
            "height_mm": V1_PAPER_HEIGHT_MM,
        },
        "elements": normalized_elements,
    }


_SNAPSHOT_NORMALIZERS = {V1_CATALOG_VERSION: _normalize_layout_v1}


def normalize_layout(payload: object) -> dict[str, Any]:
    """Validate a layout submitted for the currently published catalog."""

    return _normalize_layout_v1(payload)


def _normalize_snapshot_layout(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("打印任务中的模具标签布局无效")
    catalog_version = str(payload.get("catalog_version") or "").strip()
    normalizer = _SNAPSHOT_NORMALIZERS.get(catalog_version)
    if normalizer is None:
        raise MoldLabelLayoutError(
            f"打印任务使用不支持的模具标签目录 {catalog_version or '未知'}"
        )
    return normalizer(payload)


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def layout_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _latest(db: Session) -> MoldLabelLayoutRevision | None:
    return db.scalar(
        select(MoldLabelLayoutRevision)
        .order_by(desc(MoldLabelLayoutRevision.version))
        .limit(1)
    )


def _row_layout(row: MoldLabelLayoutRevision) -> dict[str, Any]:
    try:
        raw = json.loads(row.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MoldLabelLayoutError("保存的模具标签布局已损坏") from error
    if not isinstance(raw, dict) or raw.get("catalog_version") != row.catalog_version:
        raise MoldLabelLayoutError("保存的模具标签目录版本不一致")
    normalized = _normalize_snapshot_layout(raw)
    if layout_hash(normalized) != row.payload_hash:
        raise MoldLabelLayoutError("保存的模具标签布局校验失败")
    return normalized


def _envelope(
    row: MoldLabelLayoutRevision | None,
    *,
    layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective = layout if layout is not None else (
        _row_layout(row) if row is not None else normalize_layout(default_layout())
    )
    return {
        "version": int(row.version) if row is not None else 0,
        "layout": effective,
        "layout_hash": layout_hash(effective),
    }


def effective_layout(db: Session) -> dict[str, Any]:
    return _envelope(_latest(db))


def _history(db: Session, limit: int = 10) -> list[MoldLabelLayoutRevision]:
    return list(
        db.scalars(
            select(MoldLabelLayoutRevision)
            .order_by(desc(MoldLabelLayoutRevision.version))
            .limit(limit)
        ).all()
    )


def admin_state(db: Session) -> dict[str, Any]:
    published = effective_layout(db)
    history = _history(db)
    return {
        "catalog": catalog(),
        "published": published,
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
        canonical_json({"operation_kind": operation_kind, **payload}).encode("utf-8")
    ).hexdigest()


def _operation_replay(
    db: Session,
    operation_key: str,
    request_hash: str,
) -> MoldLabelLayoutRevision | None:
    row = db.scalar(
        select(MoldLabelLayoutRevision).where(
            MoldLabelLayoutRevision.operation_key == operation_key
        )
    )
    if row is not None and row.request_hash != request_hash:
        raise MoldLabelLayoutConflict("操作编号已用于不同的模具标签布局请求")
    return row


def _append_revision(
    db: Session,
    *,
    layout: dict[str, Any],
    operation_kind: str,
    operation_key: str,
    request_hash: str,
    actor_id: int | None,
    source_release_version: int | None = None,
) -> MoldLabelLayoutRevision:
    current = _latest(db)
    normalized = normalize_layout(layout)
    row = MoldLabelLayoutRevision(
        version=(int(current.version) if current is not None else 0) + 1,
        catalog_version=CATALOG_VERSION,
        payload_json=canonical_json(normalized),
        payload_hash=layout_hash(normalized),
        operation_kind=operation_kind,
        operation_key=operation_key,
        request_hash=request_hash,
        source_release_version=source_release_version,
        created_by=actor_id,
    )
    db.add(row)
    db.flush()
    return row


def _replace(
    db: Session,
    *,
    replacement: dict[str, Any],
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
    operation_kind: str,
    source_release_version: int | None = None,
    hash_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_layout(replacement)
    request = hash_request or {
        "layout": normalized,
        "expected_release_version": expected_release_version,
    }
    request_hash = _request_hash(operation_kind, request)
    replay = _operation_replay(db, operation_key, request_hash)
    if replay is not None:
        return {**admin_state(db), "replayed": True, "operation_kind": operation_kind}
    current = _latest(db)
    current_version = int(current.version) if current is not None else 0
    if expected_release_version != current_version:
        raise MoldLabelLayoutConflict("模具标签布局版本已变化，请重新加载")
    _append_revision(
        db,
        layout=normalized,
        operation_kind=operation_kind,
        operation_key=operation_key,
        request_hash=request_hash,
        actor_id=actor_id,
        source_release_version=source_release_version,
    )
    return {**admin_state(db), "replayed": False, "operation_kind": operation_kind}


def save_and_publish(
    db: Session,
    *,
    layout: dict[str, Any],
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    return _replace(
        db,
        replacement=layout,
        expected_release_version=expected_release_version,
        operation_key=operation_key,
        actor_id=actor_id,
        operation_kind="save_and_publish",
        hash_request={
            "layout": normalize_layout(layout),
            "expected_release_version": expected_release_version,
        },
    )


def restore_default(
    db: Session,
    *,
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    return _replace(
        db,
        replacement=default_layout(),
        expected_release_version=expected_release_version,
        operation_key=operation_key,
        actor_id=actor_id,
        operation_kind="restore_default",
        hash_request={"expected_release_version": expected_release_version},
    )


def rollback_release(
    db: Session,
    *,
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    client_request = {"expected_release_version": expected_release_version}
    request_hash = _request_hash("rollback", client_request)
    if _operation_replay(db, operation_key, request_hash) is not None:
        return {**admin_state(db), "replayed": True, "operation_kind": "rollback"}
    history = _history(db, limit=2)
    if len(history) < 2:
        raise MoldLabelLayoutConflict("当前没有上一版模具标签布局可回滚")
    source = history[1]
    return _replace(
        db,
        replacement=_row_layout(source),
        expected_release_version=expected_release_version,
        operation_key=operation_key,
        actor_id=actor_id,
        operation_kind="rollback",
        source_release_version=int(source.version),
        hash_request=client_request,
    )


def load_snapshot(
    *,
    version: int | None,
    payload_json: str | None,
    payload_hash: str | None,
) -> dict[str, Any]:
    if version is None or payload_json is None or payload_hash is None:
        raise MoldLabelLayoutError("打印任务缺少冻结的模具标签布局")
    try:
        raw = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise MoldLabelLayoutError("打印任务中的模具标签布局已损坏") from error
    normalized = _normalize_snapshot_layout(raw)
    actual_hash = layout_hash(normalized)
    if actual_hash != payload_hash:
        raise MoldLabelLayoutError("打印任务中的模具标签布局校验失败")
    return {"version": int(version), "layout": normalized, "layout_hash": actual_hash}


def layout_diff_summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {item["id"]: item for item in before["elements"]}
    new = {item["id"]: item for item in after["elements"]}
    changed = [element_id for element_id, item in new.items() if old.get(element_id) != item]
    return {"changed_count": len(changed), "changed_ids": changed}
