"""Dimension indices measure geometric spans and remain identical in exports."""
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics

from app.services.drawing_exports import _wrapped_lines, engineering_pdf, _annotation_font
from app.services.drawing_geometry import build_geometry, svg


def custom_geometry():
    return build_geometry('custom_21301634_v1', dict(
        panel_width_mm='620', panel_height_mm='470', top_cover_mm='235', bottom_cover_mm='235',
        top_fold_mm='28', bottom_fold_mm='28', left_fold_mm='26', right_fold_mm='26',
        left_wing_mm='40', right_wing_mm='40'))


def test_custom_dimension_index_measures_confirmed_spans_and_preserves_contour():
    geometry = custom_geometry()
    assert len(geometry['cut']) == 12 and len(geometry['score']) == 8
    expected = [('左侧翼', 'x', '0', '40'), ('左折边', 'x', '40', '66'),
                ('中间板面宽', 'x', '66', '686'), ('右折边', 'x', '686', '712'),
                ('右侧翼', 'x', '712', '752'), ('上盖', 'y', '0', '235'),
                ('上折边', 'y', '235', '263'), ('中间板面高', 'y', '263', '733'),
                ('下折边', 'y', '733', '761'), ('下盖', 'y', '761', '996')]
    assert [(row['name'], row['axis'], row['start_mm'], row['end_mm'])
            for row in geometry['dimension_index']] == expected
    for entry in geometry['dimension_index']:
        assert Decimal(entry['value_mm']) == Decimal(entry['end_mm'])-Decimal(entry['start_mm'])
        assert entry['value_mm'] == geometry['dimensions'][entry['name']]
        mark = next(row for row in geometry['annotations'] if row['label'] == entry['id'])
        assert mark[entry['axis']+'1'] == entry['start_mm']
        assert mark[entry['axis']+'2'] == entry['end_mm']
    center = next(row for row in geometry['panels'] if row['id'] == 'center')
    assert (center['x'], center['y'], center['width'], center['height']) == ('66','263','620','470')


def test_slotted_panel_and_slot_indices_match_actual_score_and_cut_edges():
    geometry = build_geometry('slotted_v1', dict(panel_1_mm='300.5', panel_2_mm='200.25',
        panel_3_mm='300.5', panel_4_mm='200.25', body_height_mm='180.75', top_flap_mm='100.125',
        bottom_flap_mm='100.125', glue_flap_mm='35.5', slot_width_mm='5.25'))
    for entry in geometry['dimension_index']:
        assert Decimal(entry['value_mm']) == Decimal(entry['end_mm'])-Decimal(entry['start_mm'])
        assert entry['value_mm'] == geometry['dimensions'][entry['name']]
    panel = geometry['dimension_index'][1]
    assert (panel['start_mm'], panel['end_mm']) == ('300.5', '500.75')
    slot = next(row for row in geometry['dimension_index'] if row['name'] == '槽宽')
    for value in (slot['start_mm'], slot['end_mm']):
        assert any(row['x1'] == row['x2'] == value and
                   {row['y1'], row['y2']} == {'0', '100.125'} for row in geometry['cut'])


def test_svg_and_pdf_share_index_text_and_wrap_long_identifiers():
    geometry = custom_geometry()
    svg_root = ElementTree.fromstring(svg(geometry))
    svg_texts = [node.text for node in svg_root.iter('{http://www.w3.org/2000/svg}text')]
    title = '客户自有图号-' + 'ABC123-'*22
    content = engineering_pdf(geometry, customer='天华客户测试名称'*15,
                              product='独立验证内箱'*18, number=title, revision='客户原始版次'*8,
                              thickness='七层约9mm；工厂默认，非实测', print_objects=[])
    pages = PdfReader(BytesIO(content)).pages
    text = pages[0].extract_text()
    for legend in geometry['annotation_legends']:
        assert legend['text'] in svg_texts
        assert legend['text'] in text
    for size in (9, 13):
        lines = _wrapped_lines(title, 400, size)
        assert len(lines) > 1 and ''.join(lines) == title
        assert all(pdfmetrics.stringWidth(line, _annotation_font(), size) <= 400 for line in lines)
    assert title.replace(' ', '') in ''.join(pages[0].extract_text().split())


def test_liner_indices_keep_decimal_measurements():
    geometry = build_geometry('liner_v1', {'length_mm': '620.25', 'width_mm': '470.125'})
    assert [(row['name'], row['value_mm']) for row in geometry['dimension_index']] == [
        ('衬板宽', '470.125'), ('衬板长', '620.25')]
