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

ZONE_LABEL = re.compile(r"^1F-([A-Z])-\d{3}$", re.IGNORECASE)


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

    return {
        "schema_version": 1,
        "floor_code": floor_code.upper(),
        "floor_name": "一楼",
        "map_version": _map_version(path),
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
