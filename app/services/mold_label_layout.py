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

V2_CATALOG_VERSION = "p1-103-v2"
V2_PAPER_WIDTH_MM = 80.0
V2_PAPER_HEIGHT_MM = 40.0
V2_ELEMENT_CATALOG = V1_ELEMENT_CATALOG
_V2_CATALOG_BY_ID = {item["id"]: item for item in V2_ELEMENT_CATALOG}

V3_CATALOG_VERSION = "p1-103-v3"
V3_PAPER_WIDTH_MM = 80.0
V3_PAPER_HEIGHT_MM = 40.0
V3_ELEMENT_CATALOG = V2_ELEMENT_CATALOG
_V3_CATALOG_BY_ID = {item["id"]: item for item in V3_ELEMENT_CATALOG}

V4_CATALOG_VERSION = "p1-112-v1"
V4_PAPER_WIDTH_MM = 80.0
V4_PAPER_HEIGHT_MM = 40.0
V4_ELEMENT_CATALOG = (
    {"id": "board_specification", "label": "片料尺寸", "kind": "text"},
    {"id": "product_specification", "label": "产品尺寸", "kind": "text"},
    {"id": "flute_type", "label": "楞型", "kind": "text"},
    {"id": "customer_name", "label": "客户名称", "kind": "text"},
    {"id": "mold_number", "label": "模具编号", "kind": "text"},
    {"id": "mold_qr", "label": "模具二维码", "kind": "qr"},
)
_V4_CATALOG_BY_ID = {item["id"]: item for item in V4_ELEMENT_CATALOG}

V5_CATALOG_VERSION = "p1-115-v1"
V5_PAPER_WIDTH_MM = 80.0
V5_PAPER_HEIGHT_MM = 40.0
V5_ELEMENT_CATALOG = V4_ELEMENT_CATALOG
_V5_CATALOG_BY_ID = {item["id"]: item for item in V5_ELEMENT_CATALOG}

V6_CATALOG_VERSION = "p1-117-v1"
V6_PAPER_WIDTH_MM = 80.0
V6_PAPER_HEIGHT_MM = 40.0
V6_ELEMENT_CATALOG = (
    {"id": "board_specification", "label": "片料尺寸", "kind": "text"},
    {"id": "product_specification", "label": "产品尺寸", "kind": "text"},
    {"id": "flute_type", "label": "楞型", "kind": "text"},
    {
        "id": "mold_identity",
        "label": "客户简称与模具标签名称",
        "kind": "text",
    },
    {
        "id": "mold_chinese_short_name",
        "label": "模具中文简写",
        "kind": "text",
    },
    {"id": "mold_qr", "label": "模具二维码", "kind": "qr"},
)
_V6_CATALOG_BY_ID = {item["id"]: item for item in V6_ELEMENT_CATALOG}

V7_CATALOG_VERSION = "p1-118-v1"
V7_PAPER_WIDTH_MM = 80.0
V7_PAPER_HEIGHT_MM = 40.0
V7_ELEMENT_CATALOG = (
    {"id": "cutting_mode", "label": "开料方式", "kind": "text"},
    {"id": "rack_location", "label": "模具货架位置", "kind": "text"},
    {"id": "custom_note", "label": "自定义显示内容", "kind": "text"},
    {"id": "section_rule_top", "label": "顶部与中部横线", "kind": "rule"},
    {"id": "product_name", "label": "产品名称", "kind": "text"},
    {
        "id": "customer_inventory_code",
        "label": "客户简称与存货编码",
        "kind": "text",
    },
    {"id": "mold_qr", "label": "模具二维码", "kind": "qr"},
    {"id": "section_rule_bottom", "label": "中部与底部横线", "kind": "rule"},
    {"id": "report_specification", "label": "片料尺寸", "kind": "text"},
)
_V7_CATALOG_BY_ID = {item["id"]: item for item in V7_ELEMENT_CATALOG}

# These aliases describe the catalog accepted for new writes.  Historical
# print snapshots use their own version-pinned decoder below.
V8_CATALOG_VERSION = "p1-119-v1"
CATALOG_VERSION = V8_CATALOG_VERSION
PAPER_WIDTH_MM = V7_PAPER_WIDTH_MM
PAPER_HEIGHT_MM = V7_PAPER_HEIGHT_MM
ELEMENT_CATALOG = V7_ELEMENT_CATALOG


class MoldLabelLayoutError(ValueError):
    pass


class MoldLabelLayoutConflict(MoldLabelLayoutError):
    pass


def _default_layout_v7() -> dict[str, Any]:
    """Historical V7 geometry; never change existing print snapshots."""

    return {
        "catalog_version": V7_CATALOG_VERSION,
        "paper": {"width_mm": PAPER_WIDTH_MM, "height_mm": PAPER_HEIGHT_MM},
        "elements": [
            {
                "id": "cutting_mode",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 0.6,
                "width_mm": 77.6,
                "height_mm": 4.5,
                "font_size_mm": 4.4,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "rack_location",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 5.9,
                "width_mm": 77.6,
                "height_mm": 4.5,
                "font_size_mm": 4.4,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "custom_note",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 11.2,
                "width_mm": 77.6,
                "height_mm": 4.5,
                "font_size_mm": 4.4,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "section_rule_top",
                "kind": "rule",
                "x_mm": 1.2,
                "y_mm": 16.6,
                "width_mm": 77.6,
                "height_mm": 0.2,
                "line_width_mm": 0.2,
                "color": "#111111",
                "visible": True,
            },
            {
                "id": "product_name",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 17.2,
                "width_mm": 61.8,
                "height_mm": 3.8,
                "font_size_mm": 3.6,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "customer_inventory_code",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 21.6,
                "width_mm": 61.8,
                "height_mm": 10.0,
                "font_size_mm": 6.0,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_qr",
                "kind": "qr",
                "x_mm": 64.4,
                "y_mm": 18.7,
                "width_mm": 14.2,
                "height_mm": 14.2,
                "visible": True,
            },
            {
                "id": "section_rule_bottom",
                "kind": "rule",
                "x_mm": 1.2,
                "y_mm": 34.8,
                "width_mm": 77.6,
                "height_mm": 0.2,
                "line_width_mm": 0.2,
                "color": "#111111",
                "visible": True,
            },
            {
                "id": "report_specification",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 35.0,
                "width_mm": 77.6,
                "height_mm": 5.0,
                "font_size_mm": 4.4,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
        ],
    }


def default_layout() -> dict[str, Any]:
    """V8: readable first row and an unobstructed 15 mm QR code."""
    layout = _default_layout_v7()
    layout["catalog_version"] = V8_CATALOG_VERSION
    geometry = {
        "rack_location": (1.2, .6, 50.0, 5.0),
        "cutting_mode": (53.0, .6, 25.8, 5.0),
        "custom_note": (1.2, 6.6, 77.6, 9.0),
        "product_name": (1.2, 17.2, 60.6, 4.2),
        "customer_inventory_code": (1.2, 22.0, 60.6, 11.6),
        "mold_qr": (63.6, 18.2, 15.0, 15.0),
    }
    for element in layout["elements"]:
        if element["id"] in geometry:
            element.update(zip(("x_mm", "y_mm", "width_mm", "height_mm"), geometry[element["id"]]))
        if element["id"] == "cutting_mode":
            element["text_align"] = "right"
    return layout


def _default_layout_v6() -> dict[str, Any]:
    """Keep the P1-117 default geometry available as a frozen source fact."""

    return {
        "catalog_version": V6_CATALOG_VERSION,
        "paper": {"width_mm": V6_PAPER_WIDTH_MM, "height_mm": V6_PAPER_HEIGHT_MM},
        "elements": [
            {
                "id": "board_specification",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 0.8,
                "width_mm": 61.8,
                "height_mm": 7.0,
                "font_size_mm": 5.6,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "product_specification",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 8.7,
                "width_mm": 48.0,
                "height_mm": 7.0,
                "font_size_mm": 4.8,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "flute_type",
                "kind": "text",
                "x_mm": 50.0,
                "y_mm": 8.7,
                "width_mm": 13.0,
                "height_mm": 7.0,
                "font_size_mm": 4.0,
                "font_weight": 800,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_identity",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 24.6,
                "width_mm": 61.8,
                "height_mm": 7.0,
                "font_size_mm": 6.0,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_chinese_short_name",
                "kind": "text",
                "x_mm": 1.2,
                "y_mm": 31.6,
                "width_mm": 61.8,
                "height_mm": 7.7,
                "font_size_mm": 6.0,
                "font_weight": 900,
                "text_align": "left",
                "visible": True,
            },
            {
                "id": "mold_qr",
                "kind": "qr",
                "x_mm": 64.4,
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


def _normalize_layout_v2(payload: object) -> dict[str, Any]:
    """Decode the values-only catalog used for new saves and print jobs."""

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != V2_CATALOG_VERSION:
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise MoldLabelLayoutError("标签纸张定义无效")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != V2_PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != V2_PAPER_HEIGHT_MM
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
        item["id"] for item in V2_ELEMENT_CATALOG if item["id"] not in seen
    ]
    if missing:
        raise MoldLabelLayoutError(f"布局缺少已登记元素：{','.join(missing)}")
    for index, left in enumerate(normalized_elements):
        for right in normalized_elements[index + 1 :]:
            if _rectangles_overlap(left, right):
                raise MoldLabelLayoutError(
                    f"{_V2_CATALOG_BY_ID[left['id']]['label']}与"
                    f"{_V2_CATALOG_BY_ID[right['id']]['label']}发生重叠"
                )
    order = {
        item["id"]: index for index, item in enumerate(V2_ELEMENT_CATALOG)
    }
    normalized_elements.sort(key=lambda item: order[item["id"]])
    return {
        "catalog_version": V2_CATALOG_VERSION,
        "paper": {
            "width_mm": V2_PAPER_WIDTH_MM,
            "height_mm": V2_PAPER_HEIGHT_MM,
        },
        "elements": normalized_elements,
    }


_SNAPSHOT_NORMALIZERS[V2_CATALOG_VERSION] = _normalize_layout_v2


def _normalize_element_v3(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MoldLabelLayoutError("布局中存在无效元素")
    element_id = str(raw.get("id") or "").strip()
    metadata = _V3_CATALOG_BY_ID.get(element_id)
    if metadata is None:
        raise MoldLabelLayoutError("布局中存在未登记元素")
    visible = raw.get("visible")
    if element_id == "product_specification":
        if visible is not False:
            raise MoldLabelLayoutError("当前40×80版式暂不显示产品尺寸")
    elif visible is not True:
        raise MoldLabelLayoutError(f"{metadata['label']}不能隐藏")
    compatible = dict(raw)
    compatible["visible"] = True
    normalized = _normalize_element_v1(compatible)
    normalized["visible"] = bool(visible)
    return normalized


def _normalize_layout_v3(payload: object) -> dict[str, Any]:
    """Decode the compact shared-mold catalog used for new saves/jobs."""

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != V3_CATALOG_VERSION:
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise MoldLabelLayoutError("标签纸张定义无效")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != V3_PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != V3_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError("本布局只允许80×40毫米内容区")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise MoldLabelLayoutError("标签元素必须是列表")
    normalized_elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_elements:
        normalized = _normalize_element_v3(raw)
        if normalized["id"] in seen:
            raise MoldLabelLayoutError(f"布局中存在重复元素 {normalized['id']}")
        seen.add(normalized["id"])
        normalized_elements.append(normalized)
    missing = [
        item["id"] for item in V3_ELEMENT_CATALOG if item["id"] not in seen
    ]
    if missing:
        raise MoldLabelLayoutError(f"布局缺少已登记元素：{','.join(missing)}")
    visible_elements = [item for item in normalized_elements if item["visible"]]
    for index, left in enumerate(visible_elements):
        for right in visible_elements[index + 1 :]:
            if _rectangles_overlap(left, right):
                raise MoldLabelLayoutError(
                    f"{_V3_CATALOG_BY_ID[left['id']]['label']}与"
                    f"{_V3_CATALOG_BY_ID[right['id']]['label']}发生重叠"
                )
    order = {
        item["id"]: index for index, item in enumerate(V3_ELEMENT_CATALOG)
    }
    normalized_elements.sort(key=lambda item: order[item["id"]])
    return {
        "catalog_version": V3_CATALOG_VERSION,
        "paper": {
            "width_mm": V3_PAPER_WIDTH_MM,
            "height_mm": V3_PAPER_HEIGHT_MM,
        },
        "elements": normalized_elements,
    }


_SNAPSHOT_NORMALIZERS[V3_CATALOG_VERSION] = _normalize_layout_v3


def _normalize_element_v4(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MoldLabelLayoutError("布局中存在无效元素")
    element_id = str(raw.get("id") or "").strip()
    metadata = _V4_CATALOG_BY_ID.get(element_id)
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
    if (
        normalized["x_mm"] < 0
        or normalized["y_mm"] < 0
        or normalized["width_mm"] <= 0
        or normalized["height_mm"] <= 0
        or normalized["x_mm"] + normalized["width_mm"] > V4_PAPER_WIDTH_MM
        or normalized["y_mm"] + normalized["height_mm"] > V4_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError(f"{metadata['label']}的位置或尺寸超出80×40内容区")
    if metadata["kind"] == "qr":
        if normalized["width_mm"] != 14.2 or normalized["height_mm"] != 14.2:
            raise MoldLabelLayoutError("模具二维码必须保持14.2毫米正方形")
        return normalized
    font_size = _finite_number(
        raw.get("font_size_mm"), name=f"{metadata['label']}字号"
    )
    if not 1.2 <= font_size <= 8.0:
        raise MoldLabelLayoutError(f"{metadata['label']}字号必须在1.2至8毫米之间")
    font_weight = int(_finite_number(
        raw.get("font_weight"), name=f"{metadata['label']}字重"
    ))
    if font_weight not in (400, 700, 800, 900):
        raise MoldLabelLayoutError(f"{metadata['label']}字重无效")
    text_align = str(raw.get("text_align") or "")
    if text_align not in ("left", "center", "right"):
        raise MoldLabelLayoutError(f"{metadata['label']}对齐方式无效")
    normalized.update(
        {
            "font_size_mm": font_size,
            "font_weight": font_weight,
            "text_align": text_align,
        }
    )
    return normalized


def _normalize_layout_v4(payload: object) -> dict[str, Any]:
    """Decode the single-label-parity catalog used for all new 40×80 jobs."""

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != V4_CATALOG_VERSION:
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise MoldLabelLayoutError("标签纸张定义无效")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != V4_PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != V4_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError("本布局只允许80×40毫米内容区")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise MoldLabelLayoutError("标签元素必须是列表")
    normalized_elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_elements:
        normalized = _normalize_element_v4(raw)
        if normalized["id"] in seen:
            raise MoldLabelLayoutError(f"布局中存在重复元素：{normalized['id']}")
        seen.add(normalized["id"])
        normalized_elements.append(normalized)
    missing = [item["id"] for item in V4_ELEMENT_CATALOG if item["id"] not in seen]
    if missing:
        raise MoldLabelLayoutError(f"布局缺少已登记元素：{','.join(missing)}")
    for index, left in enumerate(normalized_elements):
        for right in normalized_elements[index + 1 :]:
            if _rectangles_overlap(left, right):
                raise MoldLabelLayoutError(
                    f"{_V4_CATALOG_BY_ID[left['id']]['label']}与"
                    f"{_V4_CATALOG_BY_ID[right['id']]['label']}发生重叠"
                )
    order = {item["id"]: index for index, item in enumerate(V4_ELEMENT_CATALOG)}
    normalized_elements.sort(key=lambda item: order[item["id"]])
    return {
        "catalog_version": V4_CATALOG_VERSION,
        "paper": {
            "width_mm": V4_PAPER_WIDTH_MM,
            "height_mm": V4_PAPER_HEIGHT_MM,
        },
        "elements": normalized_elements,
    }


_SNAPSHOT_NORMALIZERS[V4_CATALOG_VERSION] = _normalize_layout_v4


def _normalize_layout_v5(payload: object) -> dict[str, Any]:
    """Validate the board-labelled catalog while reusing the V4 geometry rules."""

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != V5_CATALOG_VERSION:
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    compatible = {**payload, "catalog_version": V4_CATALOG_VERSION}
    normalized = _normalize_layout_v4(compatible)
    normalized["catalog_version"] = V5_CATALOG_VERSION
    return normalized


_SNAPSHOT_NORMALIZERS[V5_CATALOG_VERSION] = _normalize_layout_v5


def _normalize_element_v6(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MoldLabelLayoutError("布局中存在无效元素")
    element_id = str(raw.get("id") or "").strip()
    metadata = _V6_CATALOG_BY_ID.get(element_id)
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
    if (
        normalized["x_mm"] < 0
        or normalized["y_mm"] < 0
        or normalized["width_mm"] <= 0
        or normalized["height_mm"] <= 0
        or normalized["x_mm"] + normalized["width_mm"] > V6_PAPER_WIDTH_MM
        or normalized["y_mm"] + normalized["height_mm"] > V6_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError(f"{metadata['label']}的位置或尺寸超出80×40内容区")
    if metadata["kind"] == "qr":
        if normalized["width_mm"] != 14.2 or normalized["height_mm"] != 14.2:
            raise MoldLabelLayoutError("模具二维码必须保持14.2毫米正方形")
        return normalized
    font_size = _finite_number(
        raw.get("font_size_mm"), name=f"{metadata['label']}字号"
    )
    if not 1.2 <= font_size <= 8.0:
        raise MoldLabelLayoutError(f"{metadata['label']}字号必须在1.2至8毫米之间")
    font_weight = int(
        _finite_number(raw.get("font_weight"), name=f"{metadata['label']}字重")
    )
    if font_weight not in (400, 700, 800, 900):
        raise MoldLabelLayoutError(f"{metadata['label']}字重无效")
    text_align = str(raw.get("text_align") or "")
    if text_align not in ("left", "center", "right"):
        raise MoldLabelLayoutError(f"{metadata['label']}对齐方式无效")
    if (
        element_id in {"mold_identity", "mold_chinese_short_name"}
        and text_align != "left"
    ):
        raise MoldLabelLayoutError(f"{metadata['label']}必须左对齐")
    normalized.update(
        {
            "font_size_mm": font_size,
            "font_weight": font_weight,
            "text_align": text_align,
        }
    )
    return normalized


def _normalize_layout_v6(payload: object) -> dict[str, Any]:
    """Validate the split identity hierarchy used for new label jobs."""

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != V6_CATALOG_VERSION:
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise MoldLabelLayoutError("标签纸张定义无效")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != V6_PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != V6_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError("本布局只允许80×40毫米内容区")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise MoldLabelLayoutError("标签元素必须是列表")
    normalized_elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_elements:
        normalized = _normalize_element_v6(raw)
        if normalized["id"] in seen:
            raise MoldLabelLayoutError(f"布局中存在重复元素：{normalized['id']}")
        seen.add(normalized["id"])
        normalized_elements.append(normalized)
    missing = [item["id"] for item in V6_ELEMENT_CATALOG if item["id"] not in seen]
    if missing:
        raise MoldLabelLayoutError(f"布局缺少已登记元素：{','.join(missing)}")
    for index, left in enumerate(normalized_elements):
        for right in normalized_elements[index + 1 :]:
            if _rectangles_overlap(left, right):
                raise MoldLabelLayoutError(
                    f"{_V6_CATALOG_BY_ID[left['id']]['label']}与"
                    f"{_V6_CATALOG_BY_ID[right['id']]['label']}发生重叠"
                )
    order = {item["id"]: index for index, item in enumerate(V6_ELEMENT_CATALOG)}
    normalized_elements.sort(key=lambda item: order[item["id"]])
    return {
        "catalog_version": V6_CATALOG_VERSION,
        "paper": {
            "width_mm": V6_PAPER_WIDTH_MM,
            "height_mm": V6_PAPER_HEIGHT_MM,
        },
        "elements": normalized_elements,
    }


_SNAPSHOT_NORMALIZERS[V6_CATALOG_VERSION] = _normalize_layout_v6


def _normalize_element_v7(raw: object, *, v8: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise MoldLabelLayoutError("布局中存在无效元素")
    element_id = str(raw.get("id") or "").strip()
    metadata = _V7_CATALOG_BY_ID.get(element_id)
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
    if (
        normalized["x_mm"] < 0
        or normalized["y_mm"] < 0
        or normalized["width_mm"] <= 0
        or normalized["height_mm"] <= 0
        or normalized["x_mm"] + normalized["width_mm"] > V7_PAPER_WIDTH_MM
        or normalized["y_mm"] + normalized["height_mm"] > V7_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError(f"{metadata['label']}的位置或尺寸超出80×40内容区")
    if metadata["kind"] == "qr":
        qr_size = 15.0 if v8 else 14.2
        if normalized["width_mm"] != qr_size or normalized["height_mm"] != qr_size:
            raise MoldLabelLayoutError(f"模具二维码必须保持{qr_size}毫米正方形")
        if normalized["x_mm"] != (63.6 if v8 else 64.4) or normalized["y_mm"] != (18.2 if v8 else 18.7):
            raise MoldLabelLayoutError("模具二维码必须保持既定位置")
        return normalized
    if metadata["kind"] == "rule":
        line_width = _finite_number(
            raw.get("line_width_mm"), name=f"{metadata['label']}线宽"
        )
        color = str(raw.get("color") or "").strip()
        if not 0.1 <= line_width <= 1.0 or normalized["height_mm"] != line_width:
            raise MoldLabelLayoutError(f"{metadata['label']}线宽无效")
        if len(color) != 7 or not color.startswith("#") or any(
            character not in "0123456789abcdefABCDEF" for character in color[1:]
        ):
            raise MoldLabelLayoutError(f"{metadata['label']}颜色无效")
        normalized.update({"line_width_mm": line_width, "color": color})
        return normalized
    font_size = _finite_number(
        raw.get("font_size_mm"), name=f"{metadata['label']}字号"
    )
    if not 1.2 <= font_size <= 8.0:
        raise MoldLabelLayoutError(f"{metadata['label']}字号必须在1.2至8毫米之间")
    font_weight = int(
        _finite_number(raw.get("font_weight"), name=f"{metadata['label']}字重")
    )
    if font_weight not in (400, 700, 800, 900):
        raise MoldLabelLayoutError(f"{metadata['label']}字重无效")
    text_align = str(raw.get("text_align") or "")
    if text_align not in ("left", "center", "right"):
        raise MoldLabelLayoutError(f"{metadata['label']}对齐方式无效")
    normalized.update(
        {
            "font_size_mm": font_size,
            "font_weight": font_weight,
            "text_align": text_align,
        }
    )
    return normalized


def _normalize_layout_v7(payload: object, *, v8: bool = False) -> dict[str, Any]:
    """Validate the V7 operator-facing 40 x 80 mm layout."""

    if not isinstance(payload, dict):
        raise MoldLabelLayoutError("模具标签布局必须是对象")
    if payload.get("catalog_version") != (V8_CATALOG_VERSION if v8 else V7_CATALOG_VERSION):
        raise MoldLabelLayoutError("标签元素目录版本已变化，请重新加载默认布局")
    paper = payload.get("paper")
    if not isinstance(paper, dict):
        raise MoldLabelLayoutError("标签纸张定义无效")
    if (
        _finite_number(paper.get("width_mm"), name="纸张宽度")
        != V7_PAPER_WIDTH_MM
        or _finite_number(paper.get("height_mm"), name="纸张高度")
        != V7_PAPER_HEIGHT_MM
    ):
        raise MoldLabelLayoutError("本布局只允许80×40毫米内容区")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise MoldLabelLayoutError("标签元素必须是列表")
    normalized_elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_elements:
        normalized = _normalize_element_v7(raw, v8=v8)
        if normalized["id"] in seen:
            raise MoldLabelLayoutError(f"布局中存在重复元素：{normalized['id']}")
        seen.add(normalized["id"])
        normalized_elements.append(normalized)
    missing = [item["id"] for item in V7_ELEMENT_CATALOG if item["id"] not in seen]
    if missing:
        raise MoldLabelLayoutError(f"布局缺少已登记元素：{','.join(missing)}")
    for index, left in enumerate(normalized_elements):
        for right in normalized_elements[index + 1 :]:
            if _rectangles_overlap(left, right):
                raise MoldLabelLayoutError(
                    f"{_V7_CATALOG_BY_ID[left['id']]['label']}与"
                    f"{_V7_CATALOG_BY_ID[right['id']]['label']}发生重叠"
                )
    elements = {item["id"]: item for item in normalized_elements}
    top_rule = elements["section_rule_top"]
    bottom_rule = elements["section_rule_bottom"]
    for rule, expected_y in ((top_rule, 16.6), (bottom_rule, 34.8)):
        if (
            rule["x_mm"] != 1.2
            or rule["y_mm"] != expected_y
            or rule["width_mm"] != 77.6
            or rule["height_mm"] != 0.2
        ):
            raise MoldLabelLayoutError(f"{_V7_CATALOG_BY_ID[rule['id']]['label']}必须位于三段式边界")
    for element_id in ("cutting_mode", "rack_location", "custom_note"):
        element = elements[element_id]
        if element["y_mm"] < 0.6 or element["y_mm"] + element["height_mm"] > 16.6:
            raise MoldLabelLayoutError(f"{_V7_CATALOG_BY_ID[element_id]['label']}必须位于顶部16毫米段")
    for element_id in ("product_name", "customer_inventory_code", "mold_qr"):
        element = elements[element_id]
        if element["y_mm"] < 16.8 or element["y_mm"] + element["height_mm"] > 34.8:
            raise MoldLabelLayoutError(f"{_V7_CATALOG_BY_ID[element_id]['label']}必须位于中部18毫米段")
    for element_id in ("product_name", "customer_inventory_code"):
        element = elements[element_id]
        if element["x_mm"] + element["width_mm"] > elements["mold_qr"]["x_mm"]:
            raise MoldLabelLayoutError(f"{_V7_CATALOG_BY_ID[element_id]['label']}不得与模具二维码重叠")
    report = elements["report_specification"]
    if report["y_mm"] != 35.0 or report["height_mm"] != 5.0:
        raise MoldLabelLayoutError("片料尺寸必须独占底部5毫米段")
    order = {item["id"]: index for index, item in enumerate(V7_ELEMENT_CATALOG)}
    normalized_elements.sort(key=lambda item: order[item["id"]])
    return {
        "catalog_version": V8_CATALOG_VERSION if v8 else V7_CATALOG_VERSION,
        "paper": {
            "width_mm": V7_PAPER_WIDTH_MM,
            "height_mm": V7_PAPER_HEIGHT_MM,
        },
        "elements": normalized_elements,
    }


def _normalize_layout_v8(payload: object) -> dict[str, Any]:
    return _normalize_layout_v7(payload, v8=True)


_SNAPSHOT_NORMALIZERS[V7_CATALOG_VERSION] = _normalize_layout_v7
_SNAPSHOT_NORMALIZERS[V8_CATALOG_VERSION] = _normalize_layout_v8


def normalize_layout(payload: object) -> dict[str, Any]:
    """Validate a layout submitted for the currently published catalog."""

    return _normalize_layout_v8(payload)


def _upgrade_to_current_catalog(layout: dict[str, Any]) -> dict[str, Any]:
    """Project an active legacy release without changing frozen snapshots."""

    catalog_version = layout.get("catalog_version")
    if catalog_version == V8_CATALOG_VERSION:
        return _normalize_layout_v8(layout)
    normalizer = _SNAPSHOT_NORMALIZERS.get(catalog_version)
    if normalizer is None:
        raise MoldLabelLayoutError("保存的模具标签目录版本不受支持")
    normalizer(layout)
    # Projection for new jobs only. Persisted revisions and frozen print jobs
    # remain byte-for-byte intact and are decoded by their original catalog.
    return _normalize_layout_v8(default_layout())


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
    effective = _upgrade_to_current_catalog(layout) if layout is not None else (
        _upgrade_to_current_catalog(_row_layout(row))
        if row is not None
        else normalize_layout(default_layout())
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
        replacement=_upgrade_to_current_catalog(_row_layout(source)),
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
