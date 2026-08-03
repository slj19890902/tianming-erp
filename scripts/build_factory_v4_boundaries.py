from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import ezdxf
from ezdxf.addons import iterdxf
from PIL import Image, ImageDraw, ImageFont


BOUNDARY_LAYER = "05_ZONE_区域"

# V4 confirmed boundaries are expressed in the source drawing unit: metres.
# P is one logical zone with three component polygons so the passage stays open.
PROPOSALS = [
    {
        "code": "ZONE-1F-P-01",
        "source_label": "1F-P-001",
        "name": "新双色印刷机",
        "kind": "P",
        "points": [(-12.393, 2.088), (-12.393, 7.403), (1.112, 7.403), (1.112, 2.088)],
        "add_label_to_full_dxf": True,
    },
    {
        "code": "ZONE-1F-P-02",
        "source_label": "1F-P-002",
        "name": "旧印刷机",
        "kind": "P",
        "points": [(-5.557, 9.135), (-5.557, 15.837), (-2.075, 15.837), (-2.075, 9.135)],
        "add_label_to_full_dxf": True,
    },
    {
        "code": "ZONE-1F-P-03",
        "source_label": "1F-P-003",
        "name": "分纸机",
        "kind": "P",
        "points": [(0.859, 14.811), (0.859, 16.147), (4.221, 16.147), (4.221, 14.811)],
        "add_label_to_full_dxf": True,
    },
    {
        "code": "ZONE-1F-D-01",
        "source_label": "1F-D-001",
        "name": "旧印刷机印刷版货架",
        "kind": "D",
        "points": [(-16.515, 1.302), (-16.515, 4.773), (-13.722, 4.773), (-13.722, 1.302)],
        "add_label_to_full_dxf": False,
    },
    {
        "code": "ZONE-1F-D-02",
        "source_label": "1F-D-002",
        "name": "双层挂板货架（高 2m）",
        "kind": "D",
        "points": [(-16.488, 5.288), (-16.488, 9.220), (-15.376, 9.220), (-15.376, 5.288)],
        "add_label_to_full_dxf": False,
    },
    {
        "code": "ZONE-1F-OUT-E-01",
        "source_label": "1F-OUT-E-001",
        "name": "东侧室外临时装卸区（北段）",
        "kind": "OUT-E",
        "points": [(13.704, -1.903), (17.704, -1.903), (17.704, 9.089), (13.704, 9.089)],
        "add_label_to_full_dxf": True,
    },
    {
        "code": "ZONE-1F-OUT-E-02",
        "source_label": "1F-OUT-E-002",
        "name": "东侧室外临时装卸区（南段）",
        "kind": "OUT-E",
        "points": [(13.704, -7.878), (17.704, -7.878), (17.704, -4.390), (13.704, -4.390)],
        "add_label_to_full_dxf": True,
    },
    {
        "code": "ZONE-1F-OUT-S-01",
        "source_label": "1F-OUT-S-001",
        "name": "南侧室外临时装卸区",
        "kind": "OUT-S",
        "points": [(-10.800, -7.878), (13.704, -7.878), (13.704, -5.478), (-10.800, -5.478)],
        "add_label_to_full_dxf": True,
    },
]


LAYER_COLORS = {
    "walls": "#404b5a",
    "01_COLUMN_柱子": "#64748b",
    "02_FIRE_消防": "#dc2626",
    "03_MACHINE_设备": "#475569",
    "04_RACK_货架": "#7c3aed",
    "05_ZONE_区域": "#3b82f6",
    "06_PATH_通道": "#94a3b8",
}


def _load_entities(path: Path):
    reader = iterdxf.opendxf(path)
    try:
        return list(reader.modelspace())
    finally:
        reader.close()


def _polyline_points(entity) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y, *_ in entity.get_points("xy")]


def _proposal_bounds(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _proposal_dxf_color(kind: str) -> int:
    return 30 if kind == "P" else 6 if kind == "D" else 4


def _proposal_image_colors(kind: str) -> tuple[str, str]:
    if kind == "P":
        return "#f97316", "#f9731628"
    if kind == "D":
        return "#db2777", "#db277728"
    return "#0891b2", "#0891b228"


def create_overlay_dxf(output_path: Path) -> None:
    doc = ezdxf.new("R2013")
    doc.header["$INSUNITS"] = 6
    doc.layers.add(BOUNDARY_LAYER, color=30)
    modelspace = doc.modelspace()
    for proposal in PROPOSALS:
        color = _proposal_dxf_color(proposal["kind"])
        modelspace.add_lwpolyline(
            proposal["points"],
            close=True,
            dxfattribs={"layer": BOUNDARY_LAYER, "color": color},
        )
        min_x, min_y, max_x, max_y = _proposal_bounds(proposal["points"])
        modelspace.add_text(
            proposal["source_label"],
            height=0.22,
            dxfattribs={"layer": "0", "color": color},
        ).set_placement(((min_x + max_x) / 2, (min_y + max_y) / 2))
    modelspace.add_text(
        "1F BOUNDARIES CONFIRMED 2026-08-03",
        height=0.25,
        dxfattribs={"layer": "0", "color": 1},
    ).set_placement((-7.0, 17.4))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output_path)


def _entity_lines(proposal: dict, handle: int) -> list[str]:
    color = _proposal_dxf_color(proposal["kind"])
    lines = [
        "  0", "LWPOLYLINE", "  5", f"{handle:X}", "100", "AcDbEntity",
        "  8", BOUNDARY_LAYER, " 62", str(color), "100", "AcDbPolyline",
        " 90", str(len(proposal["points"])), " 70", "1",
    ]
    for x, y in proposal["points"]:
        lines.extend([" 10", f"{x:.6f}", " 20", f"{y:.6f}"])
    min_x, min_y, max_x, max_y = _proposal_bounds(proposal["points"])
    if proposal["add_label_to_full_dxf"]:
        lines.extend(
            [
                "  0", "TEXT", "  5", f"{handle + 1:X}", "100", "AcDbEntity",
                "  8", "0", " 62", str(color), "100", "AcDbText",
                " 10", f"{(min_x + max_x) / 2:.6f}", " 20", f"{(min_y + max_y) / 2:.6f}",
                " 30", "0.0", " 40", "0.220000", "  1", proposal["source_label"],
                "  7", "Standard", "100", "AcDbText",
            ]
        )
    return lines


def create_full_candidate(source_path: Path, output_path: Path) -> None:
    """Copy the source and append confirmed entities without rewriting malformed handles."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, output_path)
    raw = output_path.read_bytes()
    candidates = [
        (b"\r\n  0\r\nENDSEC\r\n  0\r\nSECTION\r\n  2\r\nOBJECTS", b"\r\n"),
        (b"\n  0\nENDSEC\n  0\nSECTION\n  2\nOBJECTS", b"\n"),
        (b"\r\n0\r\nENDSEC\r\n0\r\nSECTION\r\n2\r\nOBJECTS", b"\r\n"),
        (b"\n0\nENDSEC\n0\nSECTION\n2\nOBJECTS", b"\n"),
    ]
    matched = next(((marker, newline) for marker, newline in candidates if marker in raw), None)
    if matched is None:
        raise ValueError("Cannot find the end of the ENTITIES section")
    marker, newline = matched
    appended: list[str] = []
    handle = 0xF10000
    for proposal in PROPOSALS:
        appended.extend(_entity_lines(proposal, handle))
        handle += 2
    block = newline + newline.join(line.encode("utf-8") for line in appended)
    output_path.write_bytes(raw.replace(marker, block + marker, 1))


def create_reference_png(source_path: Path, output_path: Path) -> None:
    entities = _load_entities(source_path)
    points: list[tuple[float, float]] = []
    for entity in entities:
        if entity.dxftype() == "LWPOLYLINE":
            points.extend(_polyline_points(entity))
    for proposal in PROPOSALS:
        points.extend(proposal["points"])

    min_x = min(point[0] for point in points) - 1.0
    min_y = min(point[1] for point in points) - 1.0
    max_x = max(point[0] for point in points) + 1.0
    max_y = max(point[1] for point in points) + 1.0
    width, height = 1600, 1120
    margin_left, margin_top, margin_right, margin_bottom = 80, 90, 380, 70
    draw_width = width - margin_left - margin_right
    draw_height = height - margin_top - margin_bottom
    scale = min(draw_width / (max_x - min_x), draw_height / (max_y - min_y))

    def canvas(point: tuple[float, float]) -> tuple[int, int]:
        x, y = point
        return (
            int(margin_left + (x - min_x) * scale),
            int(height - margin_bottom - (y - min_y) * scale),
        )

    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image, "RGBA")
    font_path = r"C:\Windows\Fonts\msyh.ttc"
    bold_path = r"C:\Windows\Fonts\msyhbd.ttc"
    title_font = ImageFont.truetype(bold_path, 32)
    label_font = ImageFont.truetype(bold_path, 19)
    small_font = ImageFont.truetype(font_path, 16)
    legend_font = ImageFont.truetype(font_path, 19)

    draw.text((60, 28), "天明 ERP 1F V4：设备、货架与室外临时区确认图", fill="#0f172a", font=title_font)
    draw.text((60, 68), "橙色为 P 设备区，粉色为 D 货架，青色为东/南室外临时装卸区", fill="#334155", font=small_font)

    layer_priority = ["05_ZONE_区域", "06_PATH_通道", "walls", "01_COLUMN_柱子", "02_FIRE_消防", "03_MACHINE_设备", "04_RACK_货架"]
    for layer in layer_priority:
        for entity in entities:
            if entity.dxf.get("layer", "") != layer or entity.dxftype() != "LWPOLYLINE":
                continue
            poly = [canvas(point) for point in _polyline_points(entity)]
            if len(poly) < 2:
                continue
            color = LAYER_COLORS[layer]
            if layer == "05_ZONE_区域":
                draw.polygon(poly, fill="#3b82f61c")
            elif layer == "03_MACHINE_设备":
                draw.polygon(poly, fill="#47556945")
            elif layer == "04_RACK_货架":
                draw.polygon(poly, fill="#7c3aed55")
            draw.line(poly + ([poly[0]] if entity.closed else []), fill=color, width=3)

    for proposal in PROPOSALS:
        poly = [canvas(point) for point in proposal["points"]]
        color, fill = _proposal_image_colors(proposal["kind"])
        draw.polygon(poly, fill=fill)
        draw.line(poly + [poly[0]], fill=color, width=6)
        min_x_p, min_y_p, max_x_p, max_y_p = _proposal_bounds(proposal["points"])
        label_point = canvas(((min_x_p + max_x_p) / 2, (min_y_p + max_y_p) / 2))
        short = proposal["source_label"]
        bbox = draw.textbbox((0, 0), short, font=label_font)
        tx = label_point[0] - (bbox[2] - bbox[0]) // 2
        ty = label_point[1] - (bbox[3] - bbox[1]) // 2
        draw.rounded_rectangle((tx - 8, ty - 5, tx + bbox[2] + 8, ty + bbox[3] + 5), radius=7, fill="#ffffffdd")
        draw.text((tx, ty), short, fill=color, font=label_font)

    legend_x = width - margin_right + 25
    legend_y = 125
    draw.rounded_rectangle((legend_x - 20, legend_y - 25, width - 35, height - 65), radius=16, fill="#ffffffee", outline="#cbd5e1", width=2)
    draw.text((legend_x, legend_y), "图例与核对点", fill="#0f172a", font=label_font)
    legend_items = [
        ("#f97316", "P-001 新双色印刷机"),
        ("#f97316", "P-002 旧印刷机"),
        ("#f97316", "P-003 分纸机"),
        ("#db2777", "D-001 旧机印刷版货架"),
        ("#db2777", "D-002 双层挂板货架"),
        ("#0891b2", "OUT-E 东侧两段（外扩4m）"),
        ("#0891b2", "OUT-S 南侧一段（外扩2.4m）"),
        ("#3b82f6", "已有 05_ZONE 区域"),
        ("#475569", "已有设备实体"),
        ("#7c3aed", "已有货架实体"),
    ]
    y = legend_y + 48
    for color, text in legend_items:
        draw.rectangle((legend_x, y + 4, legend_x + 28, y + 20), fill=color)
        draw.text((legend_x + 42, y), text, fill="#334155", font=legend_font)
        y += 42
    y += 18
    notes = [
        "已确认的业务边界：",
        "1. P 三块同属 ZONE-1F-P",
        "2. P 边界按机器实际尺寸",
        "3. P-001/P-002 间通道保留",
        "4. D 边界按货架实际外沿",
        "5. D-002 双层，高度 2m",
        "6. 室外区只临放，不长期存储",
    ]
    for index, note in enumerate(notes):
        draw.text((legend_x, y), note, fill="#991b1b" if index == 0 else "#475569", font=legend_font)
        y += 36

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build confirmed P/D boundaries for the 1F V4 factory DXF")
    parser.add_argument("source", type=Path)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--full-output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    create_overlay_dxf(args.overlay)
    create_full_candidate(args.source, args.full_output)
    create_reference_png(args.source, args.reference)
    print(f"overlay={args.overlay}")
    print(f"full_output={args.full_output}")
    print(f"reference={args.reference}")


if __name__ == "__main__":
    main()
