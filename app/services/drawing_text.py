"""One server-rendered ink box for editable print text in SVG and PDF.

This renders user-entered text only; customer artwork is never reconstructed.
The resulting alpha bounds, not a font's em size, fill the declared mm box.
"""
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import hashlib
import math
import os
import unicodedata

from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfbase.ttfonts import TTFont


RENDERER = 'text_ink_bbox_v1'


@lru_cache(maxsize=2)
def _font_source(path: str, modified_ns: int, size: int):
    data = Path(path).read_bytes()
    font = TTFont('DrawingTextCoverage', BytesIO(data), subfontIndex=0)
    return data, frozenset(code for code, glyph in font.face.charToGlyph.items() if glyph), hashlib.sha256(data).hexdigest()


def render_text_artwork(obj: dict) -> tuple[bytes, dict]:
    text = str(obj.get('text') or '')
    if not text.strip() or len(text) > 200:
        raise HTTPException(422, '印刷文字须为1至200个字符，不能全部空白')
    text = text.replace('\r\n', '\n').replace('\r', '\n').expandtabs(4)
    if any(unicodedata.category(char).startswith('C') and char != '\n' for char in text):
        raise HTTPException(422, '印刷文字含不可打印控制字符，请核对原文')
    path = Path(os.getenv('ERP_DRAWING_TEXT_FONT_PATH') or
                str(Path(os.getenv('WINDIR', 'C:/Windows')) / 'Fonts/simsun.ttc'))
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > 50_000_000:
            raise ValueError('invalid font file')
        data, coverage, digest = _font_source(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except Exception as error:
        raise HTTPException(503, '印刷文字字体不可用，请配置ERP_DRAWING_TEXT_FONT_PATH指向已有TrueType字体；未自动替换字体') from error
    missing = sorted({char for char in text if char not in '\n ' and ord(char) not in coverage})
    if missing:
        raise HTTPException(422, '系统固定字体不含这些字符：' + ''.join(missing[:12]) + '；请使用已确认的文字原件图片')
    previous = obj.get('text_rendering')
    if previous and (previous.get('font_sha256') != digest or previous.get('renderer') != RENDERER):
        raise HTTPException(409, '印刷文字字体或生成规则已变化，请重新保存并核对预览后发布')
    font_size = max(32, min(2048, math.ceil(float(obj['height_mm']) * 600 / 25.4)))
    probe = ImageDraw.Draw(Image.new('L', (1, 1)))
    for _ in range(6):
        font = ImageFont.truetype(BytesIO(data), font_size, index=0)
        spacing = max(1, font_size // 5)
        left, top, right, bottom = probe.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
        width, height = right-left, bottom-top
        if width <= 8192 and height <= 8192 and width*height <= 8_000_000:
            break
        ratio = min(8192/max(width, 1), 8192/max(height, 1), math.sqrt(8_000_000/max(width*height, 1)))
        font_size = max(8, int(font_size * ratio * .99))
    else:
        raise HTTPException(422, '印刷文字图层超过像素限制，请拆分为多个文字对象')
    if width <= 0 or height <= 0:
        raise HTTPException(422, '印刷文字没有可见字形')
    image = Image.new('RGBA', (width, height))
    ImageDraw.Draw(image).multiline_text((-left, -top), text, font=font, spacing=spacing,
                                       fill=(180, 47, 40, 255), align='left')
    bounds = image.getchannel('A').getbbox()
    if bounds is None:
        raise HTTPException(422, '印刷文字没有可见字形')
    image = image.crop(bounds)
    stream = BytesIO()
    image.save(stream, 'PNG')
    content = stream.getvalue()
    if len(content) > 5_000_000:
        raise HTTPException(422, '印刷文字图层超过5MB，请拆分文字对象')
    rendered_digest = hashlib.sha256(content).hexdigest()
    if previous and previous.get('sha256') != rendered_digest:
        raise HTTPException(409, '印刷文字生成结果已变化，请重新保存并核对预览后发布')
    return content, {'renderer': RENDERER, 'font_name': list(font.getname()), 'font_index': 0,
                     'font_sha256': digest, 'pixels': list(image.size),
                     'bounds': 'visible_ink', 'line_spacing_em': '0.2',
                     'sha256': rendered_digest}
