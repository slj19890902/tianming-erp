"""Human-readable drawings generated from the same geometry as SVG preview."""
from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from app.services.drawing_geometry import print_placement, print_focus_geometry


def _wrapped_lines(text: str, width: float, font_size: float) -> list[str]:
    """Wrap CJK and unbroken customer drawing IDs at measured glyph widths."""
    rows, current = [], ''
    for character in str(text):
        if character == '\n':
            rows.append(current)
            current = ''
        elif current and pdfmetrics.stringWidth(current + character, 'STSong-Light', font_size) > width:
            rows.append(current)
            current = character
        else:
            current += character
    return [*rows, current]


def _draw_geometry(c, geometry: dict, viewport: tuple[float, float, float, float],
                   print_objects: list[dict], image_assets: dict | None) -> None:
    """Fit a display view while preserving the one world-mm placement model."""
    left, bottom, available_w, available_h = viewport
    dw, dh = float(geometry['width_mm']), float(geometry['height_mm'])
    margin = float(geometry.get('annotation_margin_mm', 0))
    bounds = geometry.get('view_bounds', {'x': -margin, 'y': -margin,
                                         'width': dw+margin*2, 'height': dh+margin*2})
    vx, vy, vw, vh = (float(bounds[key]) for key in ('x', 'y', 'width', 'height'))
    scale = min(available_w/vw, available_h/vh)
    offset_x = left + (available_w-vw*scale)/2 - vx*scale
    offset_y = bottom + (available_h-vh*scale)/2 + (vy+vh-dh)*scale
    c.setStrokeColor(colors.black)
    c.setLineWidth(.8)
    for part in geometry["cut"]:
        c.line(offset_x + float(part["x1"]) * scale, offset_y + (dh - float(part["y1"])) * scale,
               offset_x + float(part["x2"]) * scale, offset_y + (dh - float(part["y2"])) * scale)
    c.setStrokeColor(colors.HexColor("#37618a"))
    c.setDash(5, 3)
    c.setLineWidth(.55)
    for part in geometry["score"]:
        c.line(offset_x + float(part["x1"]) * scale, offset_y + (dh - float(part["y1"])) * scale,
               offset_x + float(part["x2"]) * scale, offset_y + (dh - float(part["y2"])) * scale)
    c.setDash()
    c.setFillColor(colors.HexColor('#444444'))
    c.setStrokeColor(colors.HexColor('#666666'))
    annotation_font = max(8, float(geometry.get("annotation_font_mm", 8)) * scale)
    c.setFont("STSong-Light", annotation_font)
    for mark in geometry.get("annotations", []):
        c.line(offset_x + float(mark['x1']) * scale, offset_y + (dh-float(mark['y1'])) * scale,
               offset_x + float(mark['x2']) * scale, offset_y + (dh-float(mark['y2'])) * scale)
        if mark.get('leader'):
            lead = mark['leader']
            c.line(offset_x+float(lead['x1'])*scale, offset_y+(dh-float(lead['y1']))*scale,
                   offset_x+float(lead['x2'])*scale, offset_y+(dh-float(lead['y2']))*scale)
        if mark.get('tick_mm'):
            tick = float(mark['tick_mm'])
            for suffix in ('1', '2'):
                x, y = float(mark['x'+suffix]), float(mark['y'+suffix])
                dx, dy = (tick, 0) if mark['x1'] == mark['x2'] else (0, tick)
                c.line(offset_x+(x-dx)*scale, offset_y+(dh-y+dy)*scale,
                       offset_x+(x+dx)*scale, offset_y+(dh-y-dy)*scale)
        c.saveState()
        c.translate(offset_x + float(mark['tx']) * scale, offset_y + (dh-float(mark['ty'])) * scale)
        if mark.get('vertical'):
            c.rotate(90)
        c.drawCentredString(0, 0, mark['label'])
        c.restoreState()
    for entry in geometry.get('annotation_legends', []):
        c.drawString(offset_x+float(entry['x'])*scale, offset_y+(dh-float(entry['y']))*scale, entry['text'])
    c.setStrokeColor(colors.black)
    c.setFillColor(colors.black)
    c.setFont("STSong-Light", 8)
    panels = {panel["id"]: panel for panel in geometry["panels"]}
    for index, obj in enumerate(print_objects):
        panel = panels[obj["panel_id"]]
        _, _, cx, cy, _, _, _ = print_placement(obj, panel)
        c.saveState()
        c.setStrokeColor(colors.HexColor("#b42f28"))
        c.translate(offset_x + float(cx) * scale, offset_y + (dh - float(cy)) * scale)
        c.rotate(-float(obj.get("rotation_deg", 0)))
        if geometry.get('view_kind') not in {'print_focus', 'structure_thumbnail'}:
            c.rect(-float(obj["width_mm"]) * scale / 2, -float(obj["height_mm"]) * scale / 2,
                   float(obj["width_mm"]) * scale, float(obj["height_mm"]) * scale)
        if obj["kind"] == "image":
            if not image_assets or index not in image_assets:
                raise ValueError("印刷图片原件缺失")
            c.drawImage(ImageReader(BytesIO(image_assets[index][0])),
                        -float(obj["width_mm"]) * scale / 2,
                        -float(obj["height_mm"]) * scale / 2,
                        width=float(obj["width_mm"]) * scale,
                        height=float(obj["height_mm"]) * scale,
                        preserveAspectRatio=True, anchor='c', mask='auto')
        else:
            from app.services.drawing_text import render_text_artwork
            content = image_assets[index][0] if image_assets and index in image_assets else render_text_artwork(obj)[0]
            c.drawImage(ImageReader(BytesIO(content)), -float(obj['width_mm'])*scale/2,
                        -float(obj['height_mm'])*scale/2, width=float(obj['width_mm'])*scale,
                        height=float(obj['height_mm'])*scale, preserveAspectRatio=False, mask='auto')
        c.restoreState()


def engineering_pdf(geometry: dict, *, customer: str, product: str,
                    number: str, revision: str, thickness: str,
                    print_objects: list[dict], image_assets: dict[int, tuple[bytes, str]] | None = None) -> bytes:
    """Annotated reference pages, never a 1:1 machine cutting file."""
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    page = landscape(A3 if max(float(geometry["width_mm"]), float(geometry["height_mm"])) > 900 else A4)
    output = BytesIO()
    c = canvas.Canvas(output, pagesize=page, pageCompression=1)
    c.setTitle(f"{number} {revision}")
    pw, ph = page
    margin, footer_h = 36, 100
    header_lines = []
    for line in (f"客户：{customer}    产品：{product}",
                 f"图号：{number}    版次：{revision}    单位：mm",
                 f"纸厚：{thickness}    总展开：{geometry['width_mm']} × {geometry['height_mm']} mm"):
        header_lines.extend(_wrapped_lines(line, pw-margin*2, 9))
    title_h = max(88, 30 + 14*len(header_lines))
    available_w = pw-margin*2
    available_h = ph-margin*2-title_h-footer_h

    def header(title):
        c.setStrokeColor(colors.black)
        c.setFillColor(colors.black)
        c.setLineWidth(.8)
        c.rect(margin/2, margin/2, pw-margin, ph-margin)
        c.setFont('STSong-Light', 13)
        c.drawString(margin, ph-margin, title)
        c.setFont('STSong-Light', 9)
        for index, line in enumerate(header_lines):
            c.drawString(margin, ph-margin-18-index*14, line)

    def finish_page():
        c.setFillColor(colors.black)
        c.setFont('STSong-Light', 8)
        c.drawRightString(pw-margin, margin-8, f"第 {c.getPageNumber()} 页")
        c.showPage()

    if print_objects:
        header('天明 ERP 印刷图（图文实际尺寸与定位）')
        thumbnail_w = min(210, available_w*.25)
        main_w = available_w-thumbnail_w-24
        focus = print_focus_geometry(geometry, print_objects)
        _draw_geometry(c, focus, (margin, margin+footer_h, main_w, available_h), print_objects, image_assets)
        thumbnail = {**geometry, 'view_kind': 'structure_thumbnail', 'annotations': [], 'annotation_legends': [],
                     'view_bounds': {'x': '0', 'y': '0', 'width': geometry['width_mm'], 'height': geometry['height_mm']}}
        tx = margin+main_w+24
        thumbnail_h = min(available_h-36, thumbnail_w*1.35)
        c.setFillColor(colors.black)
        c.setFont('STSong-Light', 9)
        c.drawString(tx, margin+footer_h+available_h-8, '结构缩略（定位参考）')
        _draw_geometry(c, thumbnail, (tx, margin+footer_h+available_h-24-thumbnail_h,
                                     thumbnail_w, thumbnail_h), print_objects, image_assets)
        c.setFillColor(colors.black)
        c.setFont('STSong-Light', 8)
        c.drawString(margin, margin+78, '主图：印刷内容聚焦；右侧：整图位置；完整结构及尺寸见下一页。')
        c.drawString(margin, margin+62, '实际宽高、方向和距边以标注为准；主图与缩略图使用不同显示比例，禁止量纸面推尺寸。')
        for index, obj in enumerate(print_objects[:3]):
            c.drawString(margin, margin+44-13*index,
                         f"对象{index+1}  {obj['width_mm']}×{obj['height_mm']}mm  面:{obj['panel_id']}  "
                         f"X:{obj['x_mm']} Y:{obj['y_mm']}mm  方向:{obj.get('rotation_deg', 0)}°")
        finish_page()

    header('天明 ERP 完整结构图（按标注尺寸核对）')
    _draw_geometry(c, geometry, (margin, margin+footer_h, available_w, available_h), [], image_assets)
    c.setFillColor(colors.black)
    c.setFont('STSong-Light', 8)
    c.drawString(margin, margin+78, '黑实线：切断    蓝虚线：压折；颜色仅辅助区分，以线型为准。')
    c.drawString(margin, margin+62, '此图为尺寸与工艺核对图；不得按纸面缩放量尺寸或直接作为机台程序。')
    finish_page()
    c.setFont('STSong-Light', 13)
    y = ph-margin
    for line in _wrapped_lines(f'尺寸明细｜{number} {revision}', pw-margin*2, 13):
        c.drawString(margin, y, line)
        y -= 18
    c.setFont('STSong-Light', 10)
    c.drawString(margin, y-4, '所有尺寸单位：mm；以明确标注和已确认原件为准，不按 PDF 纸面比例量取。')
    y -= 36
    for label, value in geometry.get('dimensions', {}).items():
        c.drawString(margin, y, f'{label}：{value} mm')
        y -= 19
    if print_objects:
        y -= 12
        c.drawString(margin, y, '印刷内容、所属面板及距边：')
        y -= 20
        for obj in print_objects:
            description = f"{obj.get('text') or '图片'}｜{obj['width_mm']}×{obj['height_mm']}｜{obj['panel_id']}｜X:{obj['x_mm']} Y:{obj['y_mm']}｜角度:{obj.get('rotation_deg', 0)}"
            for line in _wrapped_lines(description, pw-margin*2, 10):
                if y < margin+20:
                    finish_page()
                    c.setFont('STSong-Light', 10)
                    y = ph-margin
                    for heading in _wrapped_lines(f'印刷明细（续页）｜{number} {revision}', pw-margin*2, 10):
                        c.drawString(margin, y, heading)
                        y -= 14
                    y -= 12
                c.drawString(margin, y, line)
                y -= 18
    finish_page()
    c.save()
    return output.getvalue()
