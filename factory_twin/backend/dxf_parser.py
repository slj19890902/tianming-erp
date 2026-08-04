from __future__ import annotations

from collections import Counter
from hashlib import sha256
from pathlib import Path
import re

import ezdxf
from ezdxf.addons import iterdxf


UNIT_FACTORS_MM = {
    1: (25.4, "inch"),
    4: (1.0, "mm"),
    5: (10.0, "cm"),
    6: (1000.0, "m"),
}


def _read_insunits(path: Path) -> int | None:
    lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    for index, value in enumerate(lines[:-2]):
        if value.strip() == "$INSUNITS":
            try:
                return int(lines[index + 2].strip())
            except ValueError:
                return None
    return None


def _kind_for_layer(layer: str) -> str:
    normalized = re.sub(r"[\s_-]+", " ", layer).strip().lower()
    compact = normalized.replace(" ", "")
    if any(word in compact for word in ("column", "pillar", "立柱", "柱子")):
        return "column"
    if any(word in compact for word in ("door", "gate", "门")):
        return "door"
    if any(word in compact for word in ("exteriorwall", "outerwall", "externalwall", "外墙")):
        return "exterior_wall"
    if (
        compact in {"wall", "walls", "00base", "00base墙体"}
        or any(word in compact for word in ("wall", "墙体", "建筑轮廓"))
    ):
        return "wall"
    return "unknown"


def _round_mm(value: float, factor: float) -> float:
    return round(value * factor, 3)


def _entity_geometry(entity, factor: float) -> dict | None:
    entity_type = entity.dxftype()
    if entity_type == "LINE":
        return {
            "type": "polyline",
            "closed": False,
            "points": [
                [_round_mm(float(entity.dxf.start.x), factor), _round_mm(float(entity.dxf.start.y), factor)],
                [_round_mm(float(entity.dxf.end.x), factor), _round_mm(float(entity.dxf.end.y), factor)],
            ],
        }
    if entity_type == "LWPOLYLINE":
        points = [
            [_round_mm(float(x), factor), _round_mm(float(y), factor)]
            for x, y, *_ in entity.get_points("xy")
        ]
        if len(points) < 2:
            return None
        return {"type": "polyline", "closed": bool(entity.closed), "points": points}
    if entity_type == "CIRCLE":
        return {
            "type": "circle",
            "x_mm": _round_mm(float(entity.dxf.center.x), factor),
            "y_mm": _round_mm(float(entity.dxf.center.y), factor),
            "radius_mm": _round_mm(float(entity.dxf.radius), factor),
        }
    if entity_type == "INSERT":
        return {
            "type": "insert",
            "x_mm": _round_mm(float(entity.dxf.insert.x), factor),
            "y_mm": _round_mm(float(entity.dxf.insert.y), factor),
            "rotation_deg": round(float(entity.dxf.rotation or 0), 3),
            "block_name": str(entity.dxf.name),
        }
    return None


def _geometry_center(geometry: dict) -> tuple[float, float]:
    if geometry["type"] == "polyline":
        points = geometry["points"]
        return (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
    return float(geometry["x_mm"]), float(geometry["y_mm"])


def _geometry_extents(geometry: dict) -> tuple[float, float, float, float]:
    if geometry["type"] == "polyline":
        xs = [point[0] for point in geometry["points"]]
        ys = [point[1] for point in geometry["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    if geometry["type"] == "circle":
        radius = geometry["radius_mm"]
        return (
            geometry["x_mm"] - radius,
            geometry["y_mm"] - radius,
            geometry["x_mm"] + radius,
            geometry["y_mm"] + radius,
        )
    return (
        geometry["x_mm"],
        geometry["y_mm"],
        geometry["x_mm"],
        geometry["y_mm"],
    )


def parse_layout_dxf(path: Path, *, floor_code: str = "1F") -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    unit_code = _read_insunits(path)
    if unit_code not in UNIT_FACTORS_MM:
        raise ValueError(
            "DXF 必须声明 $INSUNITS，MVP 支持毫米、厘米、米或英寸图纸"
        )
    factor, unit_name = UNIT_FACTORS_MM[unit_code]
    warnings: list[str] = []
    try:
        ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        warnings.append("源 DXF 存在结构兼容问题，已使用只读流式解析；源文件未修改。")

    reader = iterdxf.opendxf(path)
    try:
        entities = list(reader.modelspace())
    finally:
        reader.close()

    structures: list[dict] = []
    entity_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    ignored_count = 0
    for index, entity in enumerate(entities, start=1):
        entity_counts[entity.dxftype()] += 1
        layer = str(entity.dxf.layer)
        layer_counts[layer] += 1
        geometry = _entity_geometry(entity, factor)
        if geometry is None:
            ignored_count += 1
            continue
        kind = _kind_for_layer(layer)
        source_handle = str(entity.dxf.handle or f"ENTITY-{index}")
        structures.append(
            {
                "id": f"DXF-{source_handle}",
                "source_handle": source_handle,
                "kind": kind,
                "layer": layer,
                "locked": kind in {"exterior_wall", "wall", "column"},
                "source_readonly": True,
                "geometry": geometry,
            }
        )

    if not structures:
        raise ValueError("DXF 中没有可显示的 LINE、LWPOLYLINE、CIRCLE 或 INSERT 实体")

    columns = [item for item in structures if item["kind"] == "column"]
    columns.sort(
        key=lambda item: (
            -round(_geometry_center(item["geometry"])[1], 3),
            round(_geometry_center(item["geometry"])[0], 3),
            item["source_handle"],
        )
    )
    normalized_floor = re.sub(r"[^A-Z0-9]", "", floor_code.upper()) or "1F"
    for sequence, column in enumerate(columns, start=1):
        column["column_code"] = f"COL-{normalized_floor}-{sequence:03d}"

    extents = [_geometry_extents(item["geometry"]) for item in structures]
    unknown_count = sum(item["kind"] == "unknown" for item in structures)
    if unknown_count:
        warnings.append(f"有 {unknown_count} 个实体无法可靠分类，已作为灰色参考线保留。")
    if ignored_count:
        warnings.append(f"有 {ignored_count} 个非 MVP 几何实体未导入。")

    return {
        "source": {
            "file_name": path.name,
            "sha256": sha256(path.read_bytes()).hexdigest(),
            "input_units": unit_name,
            "output_units": "mm",
            "parser": "ezdxf.iterdxf",
        },
        "floor_code": floor_code.upper(),
        "bounds_mm": {
            "min_x": min(item[0] for item in extents),
            "min_y": min(item[1] for item in extents),
            "max_x": max(item[2] for item in extents),
            "max_y": max(item[3] for item in extents),
        },
        "structures": structures,
        "warnings": warnings,
        "entity_counts": dict(sorted(entity_counts.items())),
        "layer_counts": dict(sorted(layer_counts.items())),
    }
