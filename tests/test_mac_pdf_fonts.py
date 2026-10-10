from io import BytesIO
import sys

import pytest
from fastapi import HTTPException
from pypdf import PdfReader

from app.services.cjk_fonts import MAC_CJK_FONT, font_face_index
from app.services.contract_pdf import _configured_font_path
from app.services.drawing_exports import engineering_pdf
from app.services.drawing_geometry import build_geometry
from app.services.drawing_text import render_text_artwork

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac system-font validation')


def test_mac_defaults_select_simplified_chinese_face_and_embed_pdf(monkeypatch):
    monkeypatch.delenv('ERP_CONTRACT_PDF_FONT_PATH', raising=False)
    monkeypatch.delenv('ERP_DRAWING_TEXT_FONT_PATH', raising=False)
    assert _configured_font_path() == MAC_CJK_FONT
    assert font_face_index(MAC_CJK_FONT) == 1
    content = engineering_pdf(build_geometry('liner_v1', {'length_mm': '620.25', 'width_mm': '470.125'}),
        customer='合成中文客户', product='纸箱衬板', number='MAC-TEST-001', revision='验证版',
        thickness='3毫米', print_objects=[])
    reader = PdfReader(BytesIO(content))
    assert '天明 ERP' in reader.pages[0].extract_text()
    assert '合成中文客户' in reader.pages[0].extract_text()
    fonts = [font.get_object() for page in reader.pages
             for font in page['/Resources']['/Font'].get_object().values()]
    assert any(font.get('/Subtype') == '/TrueType'
               and '/FontFile2' in font['/FontDescriptor'].get_object() for font in fonts)
    assert not any(font.get('/BaseFont') == '/STSong-Light' for font in fonts)


def test_text_face_change_still_requires_explicit_resave(monkeypatch):
    monkeypatch.delenv('ERP_DRAWING_TEXT_FONT_PATH', raising=False)
    obj = {'text': '天明ERP 简体文字', 'height_mm': '12'}
    _, metadata = render_text_artwork(obj)
    assert metadata['font_index'] == 1
    assert metadata['font_name'][0] == 'Heiti SC'
    with pytest.raises(HTTPException) as failure:
        render_text_artwork({**obj, 'text_rendering': {**metadata, 'font_index': 0}})
    assert failure.value.status_code == 409
    assert '重新保存' in failure.value.detail


def test_custom_font_size_cap_remains_unchanged(tmp_path, monkeypatch):
    font = tmp_path / 'oversize.ttf'
    with font.open('wb') as stream:
        stream.truncate(50_000_001)
    monkeypatch.setenv('ERP_DRAWING_TEXT_FONT_PATH', str(font))
    def forbidden(*args):
        raise AssertionError('oversize custom font must be refused before parsing')
    monkeypatch.setattr('app.services.drawing_text._font_source', forbidden)
    with pytest.raises(HTTPException) as failure:
        render_text_artwork({'text': '天明', 'height_mm': '12'})
    assert failure.value.status_code == 503
