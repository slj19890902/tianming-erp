"""Deterministic millimetre geometry for the three first-release structures.

This is an engineering model, not a cutting-machine program.  Every value is
explicitly supplied by the common-box design; purchasing sheet formulas are
deliberately absent.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from base64 import b64encode
import math
from xml.sax.saxutils import escape


class DrawingGeometryError(ValueError):
    pass


PARAMETER_KEYS = {
    "liner_v1": ("length_mm", "width_mm"),
    "custom_21301634_v1": ("panel_width_mm", "panel_height_mm", "top_cover_mm", "bottom_cover_mm",
                             "top_fold_mm", "bottom_fold_mm", "left_fold_mm", "right_fold_mm",
                             "left_wing_mm", "right_wing_mm"),
    "slotted_v1": ("panel_1_mm", "panel_2_mm", "panel_3_mm", "panel_4_mm", "body_height_mm",
                   "top_flap_mm", "bottom_flap_mm", "glue_flap_mm", "slot_width_mm"),
    "partition_v1": ("length_mm", "height_mm", "slot_count", "slot_width_mm", "slot_depth_mm",
                     "slot_pitch_mm", "slot_offset_mm", "slot_edge"),
    "assembly_v1": (),
}


def number(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DrawingGeometryError(f"{name}须为毫米数值") from error
    if not result.is_finite() or result <= 0 or result > 10000:
        raise DrawingGeometryError(f"{name}须大于0且不超过10000mm")
    return result


def plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def custom_parameter_suggestions(length: object, width: object, height: object) -> dict[str, str]:
    """User-confirmed 2026-09-22 starting values, never procurement inversion.

    The other-axis height and wing widths remain manual inputs. These values
    are offered explicitly and do not replace an existing editable segment.
    """
    number(length, '产品长')
    w, h = number(width, '产品宽'), number(height, '模切后箱高')
    return {'top_cover_mm': plain(w / 2), 'bottom_cover_mm': plain(w / 2),
            'left_fold_mm': plain(h), 'right_fold_mm': plain(h)}


def print_placement(obj: dict, panel: dict) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Return unrotated rect and centre; x/y specify the rotated bounds' top-left."""
    w, h = Decimal(str(obj["width_mm"])), Decimal(str(obj["height_mm"]))
    angle = Decimal(str(obj.get("rotation_deg", 0)))
    radians = math.radians(float(angle))
    bw = Decimal(str(round(abs(float(w) * math.cos(radians)) + abs(float(h) * math.sin(radians)), 6)))
    bh = Decimal(str(round(abs(float(w) * math.sin(radians)) + abs(float(h) * math.cos(radians)), 6)))
    cx = Decimal(panel["x"]) + Decimal(str(obj["x_mm"])) + bw / 2
    cy = Decimal(panel["y"]) + Decimal(str(obj["y_mm"])) + bh / 2
    return cx-w/2, cy-h/2, cx, cy, w, h, angle


def print_focus_geometry(geometry: dict, print_objects: list[dict]) -> dict:
    """Display-only crop around rotated ink boxes; all world mm coordinates stay fixed."""
    if not print_objects:
        return geometry
    panels = {panel['id']: panel for panel in geometry['panels']}
    boxes = []
    for obj in print_objects:
        panel = panels[obj['panel_id']]
        _, _, cx, cy, _, _, _ = print_placement(obj, panel)
        left = Decimal(panel['x']) + Decimal(str(obj['x_mm']))
        top = Decimal(panel['y']) + Decimal(str(obj['y_mm']))
        boxes.append((left, top, 2 * cx - left, 2 * cy - top))
    left, top = min(box[0] for box in boxes), min(box[1] for box in boxes)
    right, bottom = max(box[2] for box in boxes), max(box[3] for box in boxes)
    padding = max(Decimal('5'), max(right-left, bottom-top) * Decimal('.04'))
    return {**geometry, 'view_kind': 'print_focus', 'cut': [], 'score': [],
            'annotations': [], 'annotation_legends': [],
            'view_bounds': {'x': plain(left-padding), 'y': plain(top-padding),
                            'width': plain(right-left+2*padding), 'height': plain(bottom-top+2*padding)}}


def _segment(kind: str, start: tuple[Decimal, Decimal], end: tuple[Decimal, Decimal]) -> dict:
    if start == end:
        raise DrawingGeometryError("存在零长度线段")
    return {"kind": kind, "x1": plain(start[0]), "y1": plain(start[1]),
            "x2": plain(end[0]), "y2": plain(end[1])}


def _polyline(points: list[tuple[Decimal, Decimal]]) -> list[dict]:
    if len(points) < 4 or points[0] != points[-1]:
        raise DrawingGeometryError("切线轮廓未闭合")
    segments = [_segment("cut", a, b) for a, b in zip(points, points[1:])]
    keys = {(s["x1"], s["y1"], s["x2"], s["y2"]) for s in segments}
    if len(keys) != len(segments):
        raise DrawingGeometryError("存在重复切线")
    def intersects(a, b, c, d):
        def orient(p, q, r):
            return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
        def between(p, q, r):
            return min(p[0],q[0]) <= r[0] <= max(p[0],q[0]) and min(p[1],q[1]) <= r[1] <= max(p[1],q[1])
        o1,o2,o3,o4 = orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
        return ((o1 == 0 and between(a,b,c)) or (o2 == 0 and between(a,b,d)) or
                (o3 == 0 and between(c,d,a)) or (o4 == 0 and between(c,d,b)) or
                ((o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)))
    count = len(points) - 1
    for i in range(count):
        for j in range(i + 1, count):
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if intersects(points[i], points[i+1], points[j], points[j+1]):
                raise DrawingGeometryError("切线轮廓自交或存在悬空连接")
    return segments


def build_geometry(template: str, params: dict[str, object]) -> dict:
    """Return vector segments and dimensions in true millimetres.

    The custom template does not infer any panel dimension from the finished
    box height.  Slotted-carton flap and slot parameters must be evidenced.
    """
    if template == "partition_v1":
        from app.services.drawing_partition import partition_geometry
        return partition_geometry(params)
    if template == "assembly_v1":
        return {"template": template, "type": "assembly", "unit": "mm", "width_mm": "1", "height_mm": "1",
                "cut": [], "score": [], "panels": [], "fold_panels": [], "dimensions": {},
                "annotations": [], "dimension_index": [], "annotation_legends": [],
                "view_bounds": {"x": "0", "y": "0", "width": "1", "height": "1"}}
    z = Decimal(0)
    if template == "liner_v1":
        w = number(params.get("width_mm"), "衬板宽")
        h = number(params.get("length_mm"), "衬板长")
        points = [(z, z), (w, z), (w, h), (z, h), (z, z)]
        scores: list[dict] = []
        panels = [{"id": "face", "x": "0", "y": "0", "width": plain(w), "height": plain(h)}]
        dimensions = {"衬板长": plain(h), "衬板宽": plain(w)}
        fold_panels = [{"id": "face", "x": "0", "y": "0", "width": plain(w), "height": plain(h),
                        "parameter_keys": ["length_mm", "width_mm"]}]
    elif template == "custom_21301634_v1":
        width = number(params.get("panel_width_mm"), "中间板面宽")
        height = number(params.get("panel_height_mm"), "中间板面高")
        top = number(params.get("top_cover_mm"), "上盖")
        bottom = number(params.get("bottom_cover_mm"), "下盖")
        tf = number(params.get("top_fold_mm"), "上折边")
        bf = number(params.get("bottom_fold_mm"), "下折边")
        lf = number(params.get("left_fold_mm"), "左折边")
        rf = number(params.get("right_fold_mm"), "右折边")
        lw = number(params.get("left_wing_mm"), "左侧翼")
        rw = number(params.get("right_wing_mm"), "右侧翼")
        x0, x1, x2, x3 = lw, lw + lf, lw + lf + width, lw + lf + width + rf
        w = x3 + rw
        y0, y1, y2, y3 = top, top + tf, top + tf + height, top + tf + height + bf
        h = y3 + bottom
        # User-confirmed 2026-09-22: each cover edge meets the wing edge
        # directly at a concave right angle. No fold-width corner tabs.
        # Wings still connect to the central panel's upper/lower score ends.
        points = [(x1,z),(x2,z),(x2,y1),(w,y1),(w,y2),(x2,y2),
                  (x2,h),(x1,h),(x1,y2),(z,y2),(z,y1),(x1,y1),(x1,z)]
        scores = [
            _segment("score", (x1,y0), (x2,y0)),
            _segment("score", (x1,y1), (x2,y1)),
            _segment("score", (x1,y2), (x2,y2)),
            _segment("score", (x1,y3), (x2,y3)),
            _segment("score", (x0,y1), (x0,y2)),
            _segment("score", (x1,y1), (x1,y2)),
            _segment("score", (x2,y1), (x2,y2)),
            _segment("score", (x3,y1), (x3,y2)),
        ]
        panels = [
            {"id": "center", "x": plain(x1), "y": plain(y1),
             "width": plain(width), "height": plain(height)},
            {"id": "top", "x": plain(x1), "y": "0", "width": plain(width), "height": plain(top)},
            {"id": "bottom", "x": plain(x1), "y": plain(y3),
             "width": plain(width), "height": plain(bottom)},
        ]
        dimensions = {"中间板面宽": plain(width), "中间板面高": plain(height),
                      "上盖": plain(top), "下盖": plain(bottom), "上折边": plain(tf),
                      "下折边": plain(bf), "左折边": plain(lf), "右折边": plain(rf),
                      "左侧翼": plain(lw), "右侧翼": plain(rw)}
        fold_panels = [
            {"id": "center", "x": plain(x1), "y": plain(y1), "width": plain(width), "height": plain(height), "parameter_keys": ["panel_width_mm", "panel_height_mm"]},
            {"id": "top_fold", "x": plain(x1), "y": plain(y0), "width": plain(width), "height": plain(tf), "parameter_keys": ["top_fold_mm"]},
            {"id": "top_cover", "x": plain(x1), "y": "0", "width": plain(width), "height": plain(top), "parameter_keys": ["top_cover_mm"]},
            {"id": "bottom_fold", "x": plain(x1), "y": plain(y2), "width": plain(width), "height": plain(bf), "parameter_keys": ["bottom_fold_mm"]},
            {"id": "bottom_cover", "x": plain(x1), "y": plain(y3), "width": plain(width), "height": plain(bottom), "parameter_keys": ["bottom_cover_mm"]},
            {"id": "left_fold", "x": plain(lw), "y": plain(y1), "width": plain(lf), "height": plain(height), "parameter_keys": ["left_fold_mm"]},
            {"id": "left_wing", "x": "0", "y": plain(y1), "width": plain(lw), "height": plain(height), "parameter_keys": ["left_wing_mm"]},
            {"id": "right_fold", "x": plain(x2), "y": plain(y1), "width": plain(rf), "height": plain(height), "parameter_keys": ["right_fold_mm"]},
            {"id": "right_wing", "x": plain(x3), "y": plain(y1), "width": plain(rw), "height": plain(height), "parameter_keys": ["right_wing_mm"]},
        ]
    elif template == "slotted_v1":
        lengths = [number(params.get(f"panel_{i}_mm"), f"第{i}面宽") for i in range(1,5)]
        body = number(params.get("body_height_mm"), "箱身高")
        top = number(params.get("top_flap_mm"), "上盖片")
        bottom = number(params.get("bottom_flap_mm"), "下盖片")
        glue = number(params.get("glue_flap_mm"), "接头")
        slot = number(params.get("slot_width_mm"), "槽宽")
        if slot * 2 >= min(lengths):
            raise DrawingGeometryError("槽宽超过最窄面板")
        w = sum(lengths, glue)
        h = top + body + bottom
        # Flat blank with expressly sized notches at the panel boundaries.
        boundaries = []
        x = z
        for length in lengths[:-1]:
            x += length
            boundaries.append(x)
        points = [(z,top),(z,z)]
        for boundary in boundaries:
            points.extend([(boundary-slot/2,z),(boundary-slot/2,top),
                           (boundary+slot/2,top),(boundary+slot/2,z)])
        points.extend([(w-glue,z),(w-glue,top),(w,top),(w,top+body),
                       (w-glue,top+body),(w-glue,h)])
        for boundary in reversed(boundaries):
            points.extend([(boundary+slot/2,h),(boundary+slot/2,top+body),
                           (boundary-slot/2,top+body),(boundary-slot/2,h)])
        points.extend([(z,h),(z,top+body),(z,top)])
        scores = []
        x = z
        panels = []
        for index,length in enumerate(lengths,1):
            # Slots and the glue-tab perimeter are cuts, never duplicate scores.
            start = x + (slot/2 if index > 1 else z)
            end = x + length - (slot/2 if index < 4 else z)
            scores.extend([_segment("score", (start,top), (end,top)),
                           _segment("score", (start,top+body), (end,top+body))])
            panels.append({"id": f"panel_{index}", "x": plain(x), "y": plain(top),
                           "width": plain(length), "height": plain(body)})
            x += length
            scores.append(_segment("score",(x,top),(x,top+body)))
        dimensions = {**{f"第{index}面宽": plain(length) for index,length in enumerate(lengths,1)},
                      "箱身高": plain(body), "上盖片": plain(top), "下盖片": plain(bottom),
                      "接头": plain(glue), "槽宽": plain(slot)}
        fold_panels = []
        x = z
        for index, length in enumerate(lengths, 1):
            fold_panels.append({"id": f"wall_{index}", "x": plain(x), "y": plain(top),
                                "width": plain(length), "height": plain(body),
                                "parameter_keys": [f"panel_{index}_mm", "body_height_mm"]})
            start = x + (slot / 2 if index > 1 else z)
            end = x + length - (slot / 2 if index < 4 else z)
            for side, y, parameter in (("top", z, "top_flap_mm"), ("bottom", top + body, "bottom_flap_mm")):
                fold_panels.append({"id": f"{side}_flap_{index}", "x": plain(start), "y": plain(y),
                                    "width": plain(end-start), "height": plain(top if side == "top" else bottom),
                                    "parameter_keys": [f"panel_{index}_mm", parameter, "slot_width_mm"]})
            x += length
        fold_panels.append({"id": "glue_tab", "x": plain(x), "y": plain(top), "width": plain(glue),
                            "height": plain(body), "parameter_keys": ["glue_flap_mm", "body_height_mm"]})
    else:
        raise DrawingGeometryError("未验证的结构模板")
    cuts = _polyline(points)
    def endpoints(part):
        return ((Decimal(part['x1']), Decimal(part['y1'])), (Decimal(part['x2']), Decimal(part['y2'])))
    def on_segment(point, part):
        a, b = endpoints(part)
        return ((b[0]-a[0])*(point[1]-a[1]) == (b[1]-a[1])*(point[0]-a[0])
                and min(a[0],b[0]) <= point[0] <= max(a[0],b[0])
                and min(a[1],b[1]) <= point[1] <= max(a[1],b[1]))
    for index, score in enumerate(scores):
        a, b = endpoints(score)
        others = cuts + scores[:index] + scores[index+1:]
        if any(not any(on_segment(point, other) for other in others) for point in (a,b)):
            raise DrawingGeometryError('存在悬空压线端点')
        for other in others:
            c, d = endpoints(other)
            if a[0] == b[0] == c[0] == d[0]:
                overlap = min(max(a[1],b[1]),max(c[1],d[1])) - max(min(a[1],b[1]),min(c[1],d[1]))
            elif a[1] == b[1] == c[1] == d[1]:
                overlap = min(max(a[0],b[0]),max(c[0],d[0])) - max(min(a[0],b[0]),min(c[0],d[0]))
            else:
                continue
            if overlap > 0:
                raise DrawingGeometryError('切线或压线重复重叠')
    for part in [*cuts, *scores]:
        for x, y in ((Decimal(part["x1"]), Decimal(part["y1"])),
                     (Decimal(part["x2"]), Decimal(part["y2"]))):
            if x < 0 or y < 0 or x > w or y > h:
                raise DrawingGeometryError("线段越出展开图边界")
    gap = max(w, h) / 25
    annotations = [
        {"x1": "0", "y1": plain(-gap), "x2": plain(w), "y2": plain(-gap),
         "tx": plain(w/2), "ty": plain(-gap*Decimal('1.15')), "label": plain(w) + ' mm'},
        {"x1": plain(w+gap), "y1": "0", "x2": plain(w+gap), "y2": plain(h),
         "tx": plain(w+gap), "ty": plain(h/2), "label": plain(h) + ' mm', "vertical": True},
    ]
    # The numbered chains point to actual x/y spans. Their labels fan out
    # outside the blank so narrow folds need not contain tiny dimension text.
    horizontal, vertical = [], []
    if template == "custom_21301634_v1":
        horizontal = [("左侧翼", z, lw), ("左折边", lw, x1), ("中间板面宽", x1, x2),
                      ("右折边", x2, x3), ("右侧翼", x3, w)]
        vertical = [("上盖", z, y0), ("上折边", y0, y1), ("中间板面高", y1, y2),
                    ("下折边", y2, y3), ("下盖", y3, h)]
    elif template == "slotted_v1":
        x = z
        for index, length in enumerate(lengths, 1):
            horizontal.append((f"第{index}面宽", x, x+length))
            x += length
        horizontal.append(("接头", x, w))
        vertical = [("上盖片", z, top), ("箱身高", top, top+body), ("下盖片", top+body, h)]
    else:
        horizontal = [("衬板宽", z, w)]
        vertical = [("衬板长", z, h)]
    dimension_index = []
    font = gap * Decimal('.45')
    tick = gap / 10
    for axis, spans in (("x", horizontal), ("y", vertical)):
        for position, (label, start, end) in enumerate(spans):
            code = f"D{len(dimension_index)+1}"
            centre = (start + end) / 2
            if axis == "x":
                line = (start, h+gap, end, h+gap)
                tx, ty = (Decimal(position)+Decimal('.5'))*w/len(spans), h+gap*2
                leader = (centre, h+gap, tx, ty-font)
            else:
                line = (-gap, start, -gap, end)
                tx, ty = -gap*2, (Decimal(position)+Decimal('.5'))*h/len(spans)
                leader = (-gap, centre, tx+font, ty-font/3)
            mark = dict(zip(("x1", "y1", "x2", "y2"), map(plain, line)))
            mark.update(tx=plain(tx), ty=plain(ty), label=code,
                        leader=dict(zip(("x1", "y1", "x2", "y2"), map(plain, leader))),
                        tick_mm=plain(tick))
            annotations.append(mark)
            dimension_index.append({"id": code, "name": label, "value_mm": plain(end-start),
                                    "axis": axis, "start_mm": plain(start), "end_mm": plain(end)})
    if template == "slotted_v1":
        # Slot width refers to the two physical cut edges of the first slot.
        code = f"D{len(dimension_index)+1}"
        a, b = boundaries[0]-slot/2, boundaries[0]+slot/2
        annotations.append({"x1": plain(a), "x2": plain(b), "y1": plain(-gap/3), "y2": plain(-gap/3),
                            "tx": plain(boundaries[0]), "ty": plain(-gap/2), "label": code,
                            "tick_mm": plain(tick)})
        dimension_index.append({"id": code, "name": "槽宽", "value_mm": plain(slot),
                                "axis": "x", "start_mm": plain(a), "end_mm": plain(b)})
    legend_x = w + gap*3
    legend_step = gap * Decimal('.8')
    legends = [{"x": plain(legend_x), "y": plain(gap + legend_step*index),
                "text": f"{entry['id']}  {entry['name']}  {entry['value_mm']} mm"}
               for index, entry in enumerate(dimension_index)]
    # Text width is reserved in the view box, independently of cutting bounds.
    legend_width = max(Decimal(len(entry["text"])) * font for entry in legends)
    view_left, view_top = -gap*3, -gap*2
    view_right = legend_x + legend_width + gap
    view_bottom = max(h+gap*3, gap+legend_step*len(legends))
    for panel in fold_panels:
        px, py = Decimal(panel["x"]), Decimal(panel["y"])
        pw, ph = Decimal(panel["width"]), Decimal(panel["height"])
        panel["points_mm"] = [[plain(px), plain(py)], [plain(px+pw), plain(py)],
                              [plain(px+pw), plain(py+ph)], [plain(px), plain(py+ph)], [plain(px), plain(py)]]
    return {"template": template, "unit": "mm", "width_mm": plain(w),
            "height_mm": plain(h), "cut": cuts, "score": scores, "panels": panels,
            "fold_panels": fold_panels,
            "dimensions": dimensions, "annotations": annotations,
            "dimension_index": dimension_index, "annotation_legends": legends,
            "view_bounds": {"x": plain(view_left), "y": plain(view_top),
                            "width": plain(view_right-view_left), "height": plain(view_bottom-view_top)},
            "annotation_margin_mm": plain(gap * 3), "annotation_font_mm": plain(font)}


def svg(geometry: dict, print_objects: list[dict] | None = None,
        image_assets: dict[int, tuple[bytes, str]] | None = None) -> str:
    """Render vectors without active SVG content or external references."""
    width, height = geometry["width_mm"], geometry["height_mm"]
    margin = Decimal(geometry.get("annotation_margin_mm", "0"))
    bounds = geometry.get('view_bounds', {"x": str(-margin), "y": str(-margin),
                                        "width": str(Decimal(width)+margin*2), "height": str(Decimal(height)+margin*2)})
    vw, vh = bounds['width'], bounds['height']
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bounds["x"]} {bounds["y"]} {vw} {vh}" width="{vw}mm" height="{vh}mm">',
             '<g fill="none" stroke="black" stroke-width="0.6">']
    for part in geometry["cut"]:
        lines.append(f'<line x1="{part["x1"]}" y1="{part["y1"]}" x2="{part["x2"]}" y2="{part["y2"]}"/>')
    lines.append('</g><g fill="none" stroke="#2862a3" stroke-width="0.4" stroke-dasharray="5 3">')
    for part in geometry["score"]:
        lines.append(f'<line x1="{part["x1"]}" y1="{part["y1"]}" x2="{part["x2"]}" y2="{part["y2"]}"/>')
    lines.append('</g>')
    lines.append('<g data-layer="dimensions" fill="#444" stroke="#666" stroke-width="0.4">')
    for mark in geometry.get("annotations", []):
        lines.append(f'<line x1="{mark["x1"]}" y1="{mark["y1"]}" x2="{mark["x2"]}" y2="{mark["y2"]}"/>')
        if mark.get('leader'):
            lead = mark['leader']
            lines.append(f'<line x1="{lead["x1"]}" y1="{lead["y1"]}" x2="{lead["x2"]}" y2="{lead["y2"]}"/>')
        if mark.get('tick_mm'):
            tick = Decimal(mark['tick_mm'])
            for suffix in ('1', '2'):
                x, y = Decimal(mark['x'+suffix]), Decimal(mark['y'+suffix])
                dx, dy = (tick, Decimal(0)) if mark['x1'] == mark['x2'] else (Decimal(0), tick)
                lines.append(f'<line x1="{x-dx}" y1="{y-dy}" x2="{x+dx}" y2="{y+dy}"/>')
        rotate = f' transform="rotate(-90 {mark["tx"]} {mark["ty"]})"' if mark.get('vertical') else ''
        lines.append(f'<text x="{mark["tx"]}" y="{mark["ty"]}" text-anchor="middle" stroke="none" font-size="{geometry["annotation_font_mm"]}"{rotate}>{escape(mark["label"])}</text>')
    for entry in geometry.get('annotation_legends', []):
        lines.append(f'<text x="{entry["x"]}" y="{entry["y"]}" stroke="none" font-size="{geometry["annotation_font_mm"]}">{escape(entry["text"])}</text>')
    lines.append('</g>')
    panels = {panel["id"]: panel for panel in geometry["panels"]}
    for index, obj in enumerate(print_objects or []):
        panel = panels[obj["panel_id"]]
        x, y, cx, cy, ow, oh, angle = print_placement(obj, panel)
        if obj.get("kind") == "text":
            from app.services.drawing_text import render_text_artwork
            content = image_assets[index][0] if image_assets and index in image_assets else render_text_artwork(obj)[0]
            encoded = b64encode(content).decode('ascii')
            frame = '' if geometry.get('view_kind') == 'print_focus' else (
                f'<rect x="{plain(x)}" y="{plain(y)}" width="{plain(ow)}" height="{plain(oh)}" fill="none" stroke="#b42f28" stroke-width="0.6"/>')
            lines.append(f'<g transform="rotate({plain(angle)} {plain(cx)} {plain(cy)})">{frame}'
                         f'<image x="{plain(x)}" y="{plain(y)}" width="{plain(ow)}" height="{plain(oh)}" preserveAspectRatio="none" '
                         f'href="data:image/png;base64,{encoded}"/><title>{escape(str(obj["text"]))}</title></g>')
        elif obj.get("kind") == "image" and image_assets and index in image_assets:
            content, mime = image_assets[index]
            if mime not in {"image/png", "image/jpeg", "image/webp"}:
                raise DrawingGeometryError("不支持的印刷图片类型")
            encoded = b64encode(content).decode("ascii")
            lines.append(f'<image x="{plain(x)}" y="{plain(y)}" width="{plain(ow)}" height="{plain(oh)}" '
                         f'preserveAspectRatio="xMidYMid meet" transform="rotate({plain(angle)} {plain(cx)} {plain(cy)})" '
                         f'href="data:{mime};base64,{encoded}"/>')
    return ''.join(lines) + '</svg>'
