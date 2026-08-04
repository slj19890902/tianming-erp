from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable

import ezdxf
from ezdxf.addons import iterdxf


LAYER_KINDS = {
    "walls": "wall",
    "00_BASE": "wall",
    "01_COLUMN": "column",
    "02_FIRE": "fire",
    "03_MACHINE": "machine",
    "04_RACK": "rack",
    "05_ZONE": "zone",
    "06_PATH": "path",
}

BUSINESS_ZONES = [
    {
        "zone_code": "ZONE-1F-A",
        "short_code": "A",
        "zone_name": "入口综合缓冲区",
        "allow_stock_types": ["RAW_MATERIAL", "FINISHED_WAIT_DELIVERY"],
        "description": "原材料与成品待送的动态缓冲区。",
    },
    {
        "zone_code": "ZONE-1F-B",
        "short_code": "B",
        "zone_name": "待生产准备区",
        "allow_stock_types": ["RAW_MATERIAL", "FINISHED_TEMP"],
        "description": "生产任务已安排、尚未上线的准备区。",
    },
    {
        "zone_code": "ZONE-1F-C",
        "short_code": "C",
        "zone_name": "半成品待加工区",
        "allow_stock_types": [
            "WAIT_FORMING",
            "WAIT_DIECUT",
            "WAIT_GLUE",
            "WAIT_PACKING",
        ],
        "description": "按待成型、待模切、待粘箱和待打包状态管理。",
    },
    {
        "zone_code": "ZONE-1F-P",
        "short_code": "P",
        "zone_name": "生产设备区",
        "allow_stock_types": [],
        "description": "固定设备区，禁止生成库位。",
    },
    {
        "zone_code": "ZONE-1F-D",
        "short_code": "D",
        "zone_name": "工装版库",
        "allow_stock_types": ["PLATE", "HANGING_PLATE"],
        "description": "印刷版和挂板的专用位置。",
    },
    {
        "zone_code": "ZONE-1F-E",
        "short_code": "E",
        "zone_name": "弹性周转区",
        "allow_stock_types": ["RAW_MATERIAL", "FINISHED_TEMP", "SEMI_PRODUCT"],
        "description": "根据生产情况动态使用的周转区。",
    },
    {
        "zone_code": "ZONE-1F-OUT-E",
        "short_code": "外东",
        "zone_name": "东侧室外临时装卸区",
        "allow_stock_types": ["INBOUND_WAIT_RECEIPT", "FINISHED_WAIT_LOADING"],
        "description": (
            "东侧墙外临时放置待收货和待装车栈板；三楼成品可下楼短暂停放等待货车。"
            "区域受天气影响，不作为长期库存区。"
        ),
        "temporary_only": True,
        "weather_exposed": True,
    },
    {
        "zone_code": "ZONE-1F-OUT-S",
        "short_code": "外南",
        "zone_name": "南侧室外临时装卸区",
        "allow_stock_types": ["INBOUND_WAIT_RECEIPT", "FINISHED_WAIT_LOADING"],
        "description": (
            "南侧墙外临时放置待收货和待装车栈板；三楼成品可下楼短暂停放等待货车。"
            "区域受天气影响，不作为长期库存区。"
        ),
        "temporary_only": True,
        "weather_exposed": True,
    },
]

ZONE_LABEL = re.compile(r"^1F-([A-Z]+(?:-[A-Z]+)*)-\d{3}$", re.IGNORECASE)

STAFF_LABELS = {
    "COL-1F-001": "厂房立柱",
    "FIRE-1F-001": "消防栓",
    "EQ-1F-PRINT-NEW": "新水性印刷机",
    "E-1F-PINT-OLD": "旧印刷机",
    "粘箱机": "半自动粘箱机",
    "PACK-1F-001": "包装辅助区",
    "PATH-1F-001": "货运通道（禁堆）",
    "1F-A-001": "入口缓冲区①",
    "1F-A-002": "入口缓冲区②",
    "1F-A-003": "入口缓冲区③",
    "1F-B-001": "待生产准备区①",
    "1F-B-002": "待生产准备区②",
    "1F-C-001": "半成品待加工区①",
    "1F-C-002": "半成品待加工区②",
    "1F-D-001": "",
    "1F-D-002": "",
    "1F-E-001": "弹性周转区",
    "1F-P-001": "",
    "1F-P-002": "",
    "1F-P-003": "分纸机",
    "1F-OUT-E-001": "东侧临时装卸区①",
    "1F-OUT-E-002": "东侧临时装卸区②",
    "1F-OUT-S-001": "南侧临时装卸区",
}

STAFF_RACK_PRESENTATION = (
    {
        "target_center": (-15932, 7254),
        "display_name": "挂板双层货架",
        "map_label": "挂板架（双层）",
        "display_subtitle": "目前主要使用下层 · 层高2米",
        "staff_category": "plate_rack",
        "rack_levels": 2,
        "display_bays": 4,
        "bay_status": "visual_estimate",
        "label_x_mm": -15932,
        "label_y_mm": 7254,
        "label_rotation": -90,
    },
    {
        "target_center": (-15118, 3037),
        "display_name": "旧印刷机印刷版架",
        "map_label": "印刷版架",
        "display_subtitle": "印刷版专用",
        "staff_category": "plate_rack",
        "display_bays": 3,
        "bay_status": "visual_estimate",
        "label_x_mm": -15118,
        "label_y_mm": 3037,
    },
    {
        "target_center": (-8962, -3918),
        "display_name": "中模切机左侧模具架",
        "map_label": "左侧模具架",
        "display_subtitle": "中模切机 · 三层",
        "staff_category": "mold_rack",
        "rack_levels": 3,
        "display_bays": 3,
        "bay_status": "visual_estimate",
        "label_x_mm": -8962,
        "label_y_mm": -3918,
    },
    {
        "target_center": (-4705, -3729),
        "display_name": "中模切机上方模具架",
        "map_label": "上方模具架",
        "display_subtitle": "中模切机 · 现一层（预留二层）",
        "staff_category": "mold_rack",
        "rack_levels": 1,
        "planned_rack_levels": 2,
        "display_bays": 4,
        "bay_status": "visual_estimate",
        "label_x_mm": -4705,
        "label_y_mm": -2970,
    },
    {
        "target_center": (-1668, -3878),
        "display_name": "小模切机上方模具架",
        "map_label": "上方模具架",
        "display_subtitle": "小模切机 · 二层",
        "staff_category": "mold_rack",
        "rack_levels": 2,
        "display_bays": 2,
        "bay_status": "visual_estimate",
        "label_x_mm": -1668,
        "label_y_mm": -2860,
    },
)

STAFF_MACHINE_PRESENTATION = {
    "B4F": {
        "display_name": "旧印刷机",
        "machine_asset": "machine-printer-old-lineart-25d.webp",
        "asset_rotation": 90,
    },
    "B50": {
        "display_name": "分纸机",
        "machine_asset": "machine-slitter-lineart-25d.webp",
    },
    "B52": {
        "display_name": "新水性印刷机",
        "machine_asset": "machine-printer-new-lineart-25d.webp",
    },
    "B53": {
        "display_name": "半自动粘箱机",
        "machine_asset": "machine-gluer-semi-lineart-25d.webp",
    },
    "B54": {
        "display_name": "半自动钉箱机",
        "machine_asset": "machine-stitcher-semi-lineart-25d.webp",
        "asset_rotation": 90,
    },
    "B55": {
        "display_name": "钉箱机",
        "machine_asset": "machine-stitcher-arm-lineart-25d.webp",
        "asset_scale": 1.35,
    },
    "B56": {
        "display_name": "小模切机",
        "machine_asset": "machine-diecutter-lineart-25d.webp",
    },
    "B57": {
        "display_name": "中模切机",
        "machine_asset": "machine-diecutter-lineart-25d.webp",
    },
    "B58": {
        "display_name": "大模切机",
        "machine_asset": "machine-diecutter-lineart-25d.webp",
        "asset_rotation": 90,
    },
    "B67": {
        "display_name": "打包机",
        "machine_asset": "machine-packer-lineart-25d.webp",
    },
    "B97": {
        "display_name": "开槽老虎机",
        "map_label": "开槽老虎机",
        "label_x_mm": 8890,
        "label_y_mm": 1680,
        "machine_asset": "machine-slotter-lineart-25d.webp",
        "machine_asset_opacity": 0.46,
        "display_subtitle": "货架优先显示 · 设备仅作定位参考",
    },
}

V4_DXF_SPECIAL_PRESENTATION = {
    "B9A": {
        "kind": "freight_elevator",
        "display_name": "上方货运电梯（停用）",
        "map_label": "货梯（停用）",
        "operational": False,
        "position_status": "owner_confirmed_dxf",
    },
    "B9B": {
        "kind": "freight_elevator",
        "display_name": "下方货运电梯（使用中）",
        "map_label": "货运电梯",
        "operational": True,
        "position_status": "owner_confirmed_dxf",
    },
    "B98": {
        "display_name": "货梯旁一层货架",
        "map_label": "一层货架",
        "display_subtitle": "货架优先显示 · 下方机器仅作定位参考",
        "staff_category": "general_rack",
        "rack_levels": 1,
        "display_bays": 4,
        "bay_status": "visual_estimate",
        "position_status": "owner_confirmed_dxf",
    },
    "B99": {
        "kind": "pallet_spot",
        "display_name": "开槽老虎机下方临时栈板区",
        "map_label": "临时栈板区（2托）",
        "display_subtitle": "临时放置 · 不建立正式库位",
        "capacity_pallets": 2,
        "temporary_only": True,
        "position_status": "owner_confirmed_dxf",
    },
}

V4_STAFF_OVERLAYS = (
    {
        "id": "MOLD-OVERSIZE-WEST",
        "kind": "mold_storage",
        "closed": True,
        "points": [
            [-17000, -4400],
            [-16350, -4400],
            [-16350, 700],
            [-17000, 700],
        ],
        "display_name": "超大模具靠墙区",
        "map_label": "超大模具",
        "display_subtitle": "大模切机左侧靠墙",
        "label_x_mm": -16675,
        "label_y_mm": -1850,
        "label_rotation": -90,
    },
    {
        "id": "MOLD-OVERSIZE-SOUTH",
        "kind": "mold_storage",
        "closed": True,
        "points": [
            [-16250, -5050],
            [-12500, -5050],
            [-12500, -4550],
            [-16250, -4550],
        ],
        "display_name": "超大模具靠墙区",
        "map_label": "超大模具",
        "display_subtitle": "大模切机下方靠墙",
        "label_x_mm": -14375,
        "label_y_mm": -4800,
        "label_rotation": 0,
    },
)


def _kind_for_layer(layer: str) -> str | None:
    if layer.lower() in {"doors", "windows"}:
        return layer.lower()[:-1]
    for prefix, kind in LAYER_KINDS.items():
        if layer == prefix or layer.startswith(f"{prefix}_"):
            return kind
    return None


def _point_in_polygon(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    x, y = point
    inside = False
    for index, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(index + 1) % len(polygon)]
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def _millimetres(value: float) -> float:
    return round(value * 1000, 3)


def _points_mm(points: Iterable[tuple[float, float]]) -> list[list[float]]:
    return [[_millimetres(x), _millimetres(y)] for x, y in points]


def _apply_v4_staff_presentation(
    primitives: list[dict], labels: list[dict]
) -> list[dict]:
    for label in labels:
        raw_text = label["text"]
        display_text = STAFF_LABELS.get(raw_text)
        if display_text is None:
            display_text = raw_text if re.search(r"[\u3400-\u9fff]", raw_text) else ""
        label["display_text"] = display_text
        if raw_text == "PATH-1F-001":
            label["label_x_mm"] = 4750
            label["label_y_mm"] = 6100

    for primitive in primitives:
        special = V4_DXF_SPECIAL_PRESENTATION.get(primitive.get("id"))
        if special:
            primitive.update(special)
            if primitive.get("points"):
                x_values = [point[0] for point in primitive["points"]]
                y_values = [point[1] for point in primitive["points"]]
                primitive.setdefault("label_x_mm", round((min(x_values) + max(x_values)) / 2))
                primitive.setdefault("label_y_mm", round((min(y_values) + max(y_values)) / 2))
        if primitive.get("kind") == "machine":
            machine_presentation = STAFF_MACHINE_PRESENTATION.get(primitive["id"])
            if machine_presentation is None:
                continue
            primitive.update(
                {
                    **machine_presentation,
                    "interactive": False,
                    "storage_rule": "NO_PALLET_ON_MACHINE",
                    "position_status": "dxf_fixed_equipment",
                }
            )
        if primitive.get("kind") != "rack" or not primitive.get("points"):
            continue
        x_values = [point[0] for point in primitive["points"]]
        y_values = [point[1] for point in primitive["points"]]
        center_x = round((min(x_values) + max(x_values)) / 2)
        center_y = round((min(y_values) + max(y_values)) / 2)
        for presentation in STAFF_RACK_PRESENTATION:
            target_x, target_y = presentation["target_center"]
            if abs(center_x - target_x) > 80 or abs(center_y - target_y) > 80:
                continue
            primitive.update(
                {
                    key: value
                    for key, value in presentation.items()
                    if key != "target_center"
                }
            )
            break

    return [dict(item) for item in V4_STAFF_OVERLAYS]


def _read_header_variable(path: Path, variable: str) -> int | None:
    lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    for index, value in enumerate(lines[:-2]):
        if value.strip() != variable:
            continue
        try:
            return int(lines[index + 2].strip())
        except ValueError:
            return None
    return None


def _map_version(path: Path) -> str:
    matched = re.search(r"(?i)(?:^|[^A-Z0-9])V(\d+)(?:[^A-Z0-9]|$)", path.stem)
    return f"V{matched.group(1)}" if matched else "UNVERSIONED"


def parse_factory_dxf(path: Path, *, floor_code: str = "1F") -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)

    input_units = _read_header_variable(path, "$INSUNITS")
    if input_units != 6:
        raise ValueError(
            f"当前导入器只接受 INSUNITS=6 的米制图纸，实际为 {input_units!r}"
        )

    standard_load_compatible = True
    warnings: list[str] = []
    try:
        ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        standard_load_compatible = False
        warnings.append(
            "源 DXF 存在重复 handle，标准文档加载失败；本资产使用只读流式实体解析生成。"
        )

    reader = iterdxf.opendxf(path)
    entity_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    primitives: list[dict] = []
    labels: list[dict] = []

    try:
        entities = list(reader.modelspace())
    finally:
        reader.close()

    text_points: list[tuple[str, float, float, str]] = []
    for entity in entities:
        entity_counts[entity.dxftype()] += 1
        layer_counts[entity.dxf.layer] += 1
        if entity.dxftype() == "TEXT":
            x = float(entity.dxf.insert.x)
            y = float(entity.dxf.insert.y)
            value = str(entity.dxf.text).strip()
            text_points.append((value, x, y, entity.dxf.layer))
            labels.append(
                {
                    "text": value,
                    "x_mm": _millimetres(x),
                    "y_mm": _millimetres(y),
                    "layer": entity.dxf.layer,
                }
            )

    for entity in entities:
        layer = entity.dxf.layer
        kind = _kind_for_layer(layer)
        special = V4_DXF_SPECIAL_PRESENTATION.get(entity.dxf.handle)
        if special and special.get("kind"):
            kind = special["kind"]
        if kind is None:
            continue

        if entity.dxftype() == "LWPOLYLINE":
            points = _points_mm(
                (float(x), float(y)) for x, y, *_ in entity.get_points("xy")
            )
            if len(points) < 2:
                continue
            primitive = {
                "id": entity.dxf.handle,
                "kind": kind,
                "layer": layer,
                "closed": bool(entity.closed),
                "points": points,
            }
            if kind == "zone":
                matched = [
                    value
                    for value, x, y, _text_layer in text_points
                    if ZONE_LABEL.fullmatch(value)
                    and _point_in_polygon((_millimetres(x), _millimetres(y)), points)
                ]
                if matched:
                    primitive["source_label"] = matched[0]
                    short_code = ZONE_LABEL.fullmatch(matched[0]).group(1).upper()
                    primitive["zone_code"] = f"ZONE-{floor_code.upper()}-{short_code}"
            primitives.append(primitive)
        elif entity.dxftype() == "LINE":
            primitives.append(
                {
                    "id": entity.dxf.handle,
                    "kind": kind,
                    "layer": layer,
                    "closed": False,
                    "points": _points_mm(
                        [
                            (float(entity.dxf.start.x), float(entity.dxf.start.y)),
                            (float(entity.dxf.end.x), float(entity.dxf.end.y)),
                        ]
                    ),
                }
            )
        elif entity.dxftype() == "INSERT" and kind in {"door", "window"}:
            primitives.append(
                {
                    "id": entity.dxf.handle,
                    "kind": kind,
                    "layer": layer,
                    "block_name": entity.dxf.name,
                    "x_mm": _millimetres(float(entity.dxf.insert.x)),
                    "y_mm": _millimetres(float(entity.dxf.insert.y)),
                    "rotation": round(float(entity.dxf.rotation), 4),
                }
            )

    point_values = [
        coordinate
        for primitive in primitives
        for point in primitive.get("points", [])
        for coordinate in [point]
    ]
    x_values = [point[0] for point in point_values] + [
        primitive["x_mm"] for primitive in primitives if "x_mm" in primitive
    ]
    y_values = [point[1] for point in point_values] + [
        primitive["y_mm"] for primitive in primitives if "y_mm" in primitive
    ]
    if not x_values or not y_values:
        raise ValueError("DXF 中没有可显示的工厂地图实体")

    source_zone_codes = {
        primitive.get("zone_code")
        for primitive in primitives
        if primitive["kind"] == "zone" and primitive.get("zone_code")
    }
    unlabeled_zone_count = sum(
        1
        for primitive in primitives
        if primitive["kind"] == "zone" and not primitive.get("zone_code")
    )
    if unlabeled_zone_count:
        warnings.append(f"05_ZONE_区域 中有 {unlabeled_zone_count} 个闭合区域没有区域编号。")
    for zone in BUSINESS_ZONES:
        zone["boundary_status"] = (
            "drawn" if zone["zone_code"] in source_zone_codes else "missing"
        )
    missing_priority_zones = [
        code for code in ("ZONE-1F-D", "ZONE-1F-P") if code not in source_zone_codes
    ]
    if missing_priority_zones:
        warnings.append(
            f"{ '、'.join(missing_priority_zones) } 在 05_ZONE_区域 中没有独立边界；"
            "仅显示对应货架或设备对象。"
        )
    missing_outdoor_zones = [
        code
        for code in ("ZONE-1F-OUT-E", "ZONE-1F-OUT-S")
        if code not in source_zone_codes
    ]
    if missing_outdoor_zones:
        warnings.append(
            "东侧、南侧室外临时装卸区的业务用途已确认，但 DXF 没有厂区外边界和"
            "外扩尺寸；当前只登记区域，不生成推测轮廓或库位。"
        )

    text_values = {item[0] for item in text_points}
    if "EQ-1F-SLITTER-01" not in text_values:
        warnings.append("图纸中未找到设备编号 EQ-1F-SLITTER-01。")
    if "E-1F-PINT-OLD" in text_values:
        warnings.append("图纸保留原始设备文字 E-1F-PINT-OLD，疑似 PRINT 拼写待现场确认。")

    map_version = _map_version(path)
    staff_overlays: list[dict] = []
    presentation: dict | None = None
    if map_version == "V4":
        staff_overlays = _apply_v4_staff_presentation(primitives, labels)
        presentation = {
            "mode": "employee_floor_plan",
            "language": "zh-CN",
            "wall_snap_ratio": 0.12,
            "door_symbol": "plan_swing",
            "window_symbol": "double_line",
        }

    payload = {
        "schema_version": 1,
        "floor_code": floor_code.upper(),
        "floor_name": "一楼",
        "map_version": map_version,
        "source": {
            "file_name": path.name,
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "input_units": "m",
            "output_units": "mm",
            "parser": "ezdxf.iterdxf",
            "standard_load_compatible": standard_load_compatible,
        },
        "bounds_mm": {
            "min_x": min(x_values),
            "min_y": min(y_values),
            "max_x": max(x_values),
            "max_y": max(y_values),
        },
        "entity_counts": dict(sorted(entity_counts.items())),
        "layer_counts": dict(sorted(layer_counts.items())),
        "business_zones": BUSINESS_ZONES,
        "primitives": primitives,
        "labels": labels,
        "warnings": warnings,
    }
    if presentation:
        payload["presentation"] = presentation
        payload["staff_overlays"] = staff_overlays
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将天明 ERP LibreCAD 工厂 DXF 转换为只读地图 JSON。"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--floor-code", default="1F")
    args = parser.parse_args()

    payload = parse_factory_dxf(args.source, floor_code=args.floor_code)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "floor_code": payload["floor_code"],
                "primitives": len(payload["primitives"]),
                "warnings": len(payload["warnings"]),
                "source_sha256": payload["source"]["sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
