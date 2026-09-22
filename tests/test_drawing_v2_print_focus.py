"""Print focus changes display bounds only, retaining mm placement and frozen files."""
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import json
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest
from fastapi import HTTPException
from PIL import Image
from pypdf import PdfReader
from sqlalchemy.orm import Session

import app.api.drawing_design as api
from app.api.drawing_design import DesignWrite, PublishWrite
from app.models.drawing_design import DrawingRelease
from app.models.user import User
from app.services.drawing_exports import engineering_pdf
from app.services.drawing_geometry import build_geometry, print_focus_geometry, print_placement, svg
from test_drawing_v2_isolated import isolated_drawing_db


def custom_geometry():
    return build_geometry('custom_21301634_v1', dict(panel_width_mm=620, panel_height_mm=470,
        top_cover_mm=235, bottom_cover_mm=235, top_fold_mm=28, bottom_fold_mm=28,
        left_fold_mm=26, right_fold_mm=26, left_wing_mm=40, right_wing_mm=40))


def test_rotated_multi_panel_focus_preserves_world_mm_and_unchanged_geometry():
    geometry = custom_geometry()
    before = deepcopy(geometry)
    objects = [dict(kind='image', panel_id='center', x_mm='20.25', y_mm='30.5',
                    width_mm='80.5', height_mm='20.25', rotation_deg='33'),
               dict(kind='image', panel_id='top', x_mm='100.25', y_mm='10.5',
                    width_mm='40.25', height_mm='15.5', rotation_deg='90')]
    frozen_objects = deepcopy(objects)
    stream = BytesIO()
    Image.new('RGBA', (80, 20), (10, 90, 30, 128)).save(stream, 'PNG')
    assets = {index: (stream.getvalue(), 'image/png') for index in range(2)}
    focus = print_focus_geometry(geometry, objects)
    bounds = {key: Decimal(value) for key, value in focus['view_bounds'].items()}
    panels = {p['id']: p for p in geometry['panels']}
    assert float(bounds['width']) < float(geometry['width_mm'])
    assert float(bounds['height']) < float(geometry['height_mm'])
    for obj in objects:
        panel = panels[obj['panel_id']]
        _, _, cx, cy, _, _, _ = print_placement(obj, panel)
        x,y = Decimal(panel['x'])+Decimal(obj['x_mm']),Decimal(panel['y'])+Decimal(obj['y_mm'])
        assert bounds['x'] < x < 2*cx-x < bounds['x']+bounds['width']
        assert bounds['y'] < y < 2*cy-y < bounds['y']+bounds['height']
    focused = ElementTree.fromstring(svg(focus, objects, assets))
    preview = ElementTree.fromstring(svg(geometry, objects, assets))
    namespace = '{http://www.w3.org/2000/svg}'
    assert not list(focused.iter(namespace+'line')) and not list(focused.iter(namespace+'text'))
    assert list(preview.iter(namespace+'line'))
    assert [n.attrib for n in focused.iter(namespace+'image')] == [n.attrib for n in preview.iter(namespace+'image')]
    assert geometry == before and objects == frozen_objects
    assert focus['panels'] == geometry['panels'] and focus['dimensions'] == geometry['dimensions']
    assert print_focus_geometry(geometry, []) is geometry
    assert svg(print_focus_geometry(geometry, [])) == svg(geometry)


def test_print_pdf_has_focus_thumbnail_then_full_structure_and_details():
    geometry = custom_geometry()
    obj = dict(kind='text', text='天明 ERP\nTM 620', panel_id='center', x_mm='20.25', y_mm='30.5',
               width_mm='120.5', height_mm='34.25', rotation_deg='0')
    content = engineering_pdf(geometry, customer='隔离测试', product='印刷主视图验证', number='FOCUS-620',
        revision='R02', thickness='AB 7mm 工厂默认', print_objects=[obj])
    pages = PdfReader(BytesIO(content)).pages
    assert len(pages) == 3
    text = [page.extract_text() for page in pages]
    assert '印刷图' in text[0] and '结构缩略' in text[0] and '120.5×34.25mm' in text[0]
    assert '完整结构图' in text[1] and '尺寸明细' in text[2]
    for entry in geometry['annotation_legends']:
        assert entry['text'] not in text[0] and entry['text'] in text[1]
    for index, page_text in enumerate(text, start=1):
        assert 'FOCUS-620' in page_text and 'R02' in page_text and f'第 {index} 页' in page_text
    assert 'X:20.25 Y:30.5' in text[2]
    plain = engineering_pdf(geometry, customer='隔离测试', product='无印刷', number='NO-PRINT',
                            revision='R00', thickness='7mm', print_objects=[])
    no_print = PdfReader(BytesIO(plain)).pages
    assert len(no_print) == 2 and '完整结构图' in no_print[0].extract_text()


def test_publication_freezes_focus_but_draft_remains_full_structure(isolated_drawing_db, monkeypatch):
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        obj = dict(kind='text', text='TM 620', panel_id='face', x_mm='5.25', y_mm='7.5',
                   width_mm='20.5', height_mm='10.25', rotation_deg=33)
        api.save_design(ids[0], DesignWrite(expected_product_version=1, template_key='liner_v1',
                                          parameters={}, print_objects=[obj]), db, user)
        draft_svg = ElementTree.fromstring(api.preview_design(ids[0], db, user).body)
        published = api.publish_design(ids[0], PublishWrite(expected_product_version=1, expected_design_version=1,
                                                           idempotency_key='print-focus-publish-001'), db, user)
        release = db.get(DrawingRelease, published['id'])
        manifest = json.loads(release.manifest_json)
        structure = api.release_svg(ids[0], release.id, 'structure', db, user).body
        printed = api.release_svg(ids[0], release.id, 'print', db, user).body
        printed_root = ElementTree.fromstring(printed)
        expected = print_focus_geometry(manifest['geometry'], manifest['print_objects'])['view_bounds']
        assert manifest['print_view'] == {'kind': 'print_focus', 'view_bounds': expected}
        assert printed_root.attrib['viewBox'] == ' '.join(expected[key] for key in ('x','y','width','height'))
        assert draft_svg.attrib['viewBox'] == ElementTree.fromstring(structure).attrib['viewBox']
        assert manifest['geometry']['cut'] and manifest['geometry']['annotations']
        assert manifest['print_objects'][0]['x_mm'] == '5.25' and manifest['print_objects'][0]['width_mm'] == '20.5'
        def forbidden(*args, **kwargs):
            raise AssertionError('published view was regenerated')
        monkeypatch.setattr(api, 'print_focus_geometry', forbidden)
        assert api.release_svg(ids[0], release.id, 'print', db, user).body == printed


@pytest.mark.parametrize('oversized_part', ['asset', 'original'])
def test_oversized_frozen_artwork_is_rejected_before_reading(monkeypatch, oversized_part):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', 'explicit-test-root')
    large = Mock()
    large.is_file.return_value = True
    large.stat.return_value = SimpleNamespace(st_size=5_000_001)
    large.read_bytes.side_effect = AssertionError('oversized file must not be read')
    small = Mock()
    small.is_file.return_value = True
    small.stat.return_value = SimpleNamespace(st_size=4)
    small.read_bytes.return_value = b'safe'
    monkeypatch.setattr(api, 'resolve_stored_reference', lambda reference: large if reference == oversized_part else small)
    digest = hashlib.sha256(b'safe').hexdigest()
    obj = dict(kind='image', asset_reference='asset', asset_sha256=digest, asset_mime='image/png',
               original_reference='original', original_sha256=digest)
    with pytest.raises(HTTPException) as failure:
        api._released_assets([obj])
    assert failure.value.status_code == 503 and '5MB' in failure.value.detail
    large.read_bytes.assert_not_called()
