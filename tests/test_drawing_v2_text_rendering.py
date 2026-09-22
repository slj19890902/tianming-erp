"""Real glyph bounds, shared SVG/PDF pixels, and isolated publication integrity."""
import base64
import hashlib
import json
import math
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi import HTTPException
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream
from sqlalchemy.orm import Session

import app.api.drawing_design as api
import app.services.drawing_text as text_service
from app.api.drawing_design import DesignWrite, PublishWrite
from app.models.drawing_design import DrawingDesign, DrawingRelease
from app.models.user import User
from app.services.drawing_exports import engineering_pdf
from app.services.drawing_geometry import build_geometry, svg, print_focus_geometry
from app.services.drawing_snapshots import release_paper_snapshot
from app.services.secure_uploads import resolve_stored_reference
from scripts.drawing_v2_reconcile import audit
from test_drawing_v2_isolated import isolated_drawing_db


@pytest.fixture(autouse=True)
def known_font(monkeypatch):
    font = Path('C:/Windows/Fonts/simsun.ttc')
    if not font.is_file():
        pytest.skip('Existing SimSun is unavailable; no installation permitted')
    monkeypatch.setenv('ERP_DRAWING_TEXT_FONT_PATH', str(font))


def text_object(text='天明ERP\nAgj', **changes):
    return dict(kind='text', text=text, panel_id='face', x_mm='7.75', y_mm='5.50',
                width_mm='22.50', height_mm='11.25', rotation_deg='0', **changes)


def test_visible_alpha_bounds_mixed_text_descenders_and_explicit_lines():
    multiline, metadata = text_service.render_text_artwork(text_object())
    with Image.open(BytesIO(multiline)) as image:
        assert image.mode == 'RGBA'
        assert image.getchannel('A').getbbox() == (0, 0, image.width, image.height)
        assert metadata['pixels'] == list(image.size)
        assert metadata['bounds'] == 'visible_ink'
        assert image.width * image.height <= 8_000_000
        assert image.width <= 8192 and image.height <= 8192
        occupied = [y for y in range(image.height) if image.getchannel('A').crop((0, y, image.width, y+1)).getbbox()]
        gaps = [b-a for a, b in zip(occupied, occupied[1:])]
        assert max(gaps) > 2  # Two separately rendered lines, not collapsed whitespace.
    assert metadata['sha256'] == hashlib.sha256(multiline).hexdigest()
    assert len(metadata['font_sha256']) == 64
    assert metadata['font_name'][0] == 'SimSun'
    assert text_service.render_text_artwork(text_object('天明ERP\r\nAgj'))[0] == multiline
    capital, _ = text_service.render_text_artwork(text_object('ABC'))
    descender, _ = text_service.render_text_artwork(text_object('Agj'))
    with Image.open(BytesIO(capital)) as cap, Image.open(BytesIO(descender)) as desc:
        assert desc.height > cap.height  # Real descenders survive tight alpha cropping.


@pytest.mark.parametrize('text,expected', [('   \n', '空白'), ('😀', '不含'), ('A\x00B', '控制字符')])
def test_empty_missing_glyph_and_control_text_are_rejected(text, expected):
    with pytest.raises(HTTPException) as invalid:
        text_service.render_text_artwork(text_object(text))
    assert invalid.value.status_code == 422
    assert expected in invalid.value.detail


def test_missing_or_invalid_font_never_falls_back(tmp_path, monkeypatch):
    for font in (tmp_path/'missing.ttf', tmp_path/'bad.ttf'):
        if font.name == 'bad.ttf':
            font.write_bytes(b'not a font')
        monkeypatch.setenv('ERP_DRAWING_TEXT_FONT_PATH', str(font))
        with pytest.raises(HTTPException) as invalid:
            text_service.render_text_artwork(text_object())
        assert invalid.value.status_code == 503 and '未自动替换字体' in invalid.value.detail


def multiply(left, right):
    a,b,c,d,e,f = left
    g,h,i,j,k,l = right
    return (a*g+c*h, b*g+d*h, a*i+c*j, b*i+d*j, a*k+c*l+e, b*k+d*l+f)


@pytest.mark.parametrize('angle', [90, 33])
def test_svg_pdf_use_identical_ink_pixels_and_rotated_mm_placement(angle):
    geometry = build_geometry('liner_v1', dict(length_mm='100.25', width_mm='80.50'))
    obj = {**text_object(), 'rotation_deg': str(angle)}
    png, _ = text_service.render_text_artwork(obj)
    assets = {0: (png, 'image/png')}
    root = ElementTree.fromstring(svg(print_focus_geometry(geometry, [obj]), [obj], assets))
    namespace = {'s': 'http://www.w3.org/2000/svg'}
    image_node = root.find('.//s:image', namespace)
    group = next(group for group in root.findall('s:g', namespace) if image_node in list(group))
    assert base64.b64decode(image_node.attrib['href'].split(',', 1)[1]) == png
    assert image_node.attrib['preserveAspectRatio'] == 'none'
    assert image_node.attrib['width'] == '22.5' and image_node.attrib['height'] == '11.25'
    x,y = float(image_node.attrib['x']), float(image_node.attrib['y'])
    w,h = float(obj['width_mm']), float(obj['height_mm'])
    radians = math.radians(angle)
    bw,bh = abs(w*math.cos(radians))+abs(h*math.sin(radians)), abs(w*math.sin(radians))+abs(h*math.cos(radians))
    cx,cy = float(obj['x_mm'])+bw/2, float(obj['y_mm'])+bh/2
    assert (x+w/2, y+h/2) == pytest.approx((cx,cy), abs=1e-6)
    transform_values = group.attrib['transform'].removeprefix('rotate(').removesuffix(')').split()
    assert list(map(float, transform_values)) == pytest.approx([angle,cx,cy], abs=1e-6)

    pdf = engineering_pdf(geometry, customer='隔离测试', product='文字字形', number='TEXT-UAT',
        revision='R00', thickness='3mm', print_objects=[obj], image_assets=assets)
    reader = PdfReader(BytesIO(pdf))
    page = reader.pages[0]
    xobjects = page['/Resources']['/XObject'].get_object()
    assert len(xobjects) == 1
    pdf_image = next(iter(xobjects.values())).get_object()
    with Image.open(BytesIO(png)) as original:
        assert (pdf_image['/Width'], pdf_image['/Height']) == original.size
        assert pdf_image.get_data() == original.convert('RGB').tobytes()
        assert pdf_image['/SMask'].get_object().get_data() == original.getchannel('A').tobytes()
    operations = ContentStream(page.get_contents(), reader).operations
    bounds = print_focus_geometry(geometry, [obj])['view_bounds']
    vx,vy,vw,vh = (float(bounds[key]) for key in ('x','y','width','height'))
    pw,ph = float(page.mediabox.width),float(page.mediabox.height)
    main_width = pw-72-min(210,(pw-72)*.25)-24
    available_height = ph-72-88-100
    scale = min(main_width/vw, available_height/vh)
    ox = 36+(main_width-vw*scale)/2-vx*scale
    oy = 136+(available_height-vh*scale)/2+(vy+vh-float(geometry['height_mm']))*scale
    matrix, stack, image_matrices = (1,0,0,1,0,0), [], []
    for values, op in operations:
        if op == b'q': stack.append(matrix)
        elif op == b'Q': matrix = stack.pop()
        elif op == b'cm': matrix = multiply(matrix, tuple(map(float,values)))
        elif op == b'Do': image_matrices.append(matrix)
    assert len(image_matrices) == 2  # Main ink view and the positioned structural thumbnail.
    image_matrix = image_matrices[0]
    a,b,c,d,e,f = image_matrix
    for u,v,sx,sy in [(0,1,x,y),(1,1,x+w,y),(0,0,x,y+h),(1,0,x+w,y+h)]:
        rx = cx+(sx-cx)*math.cos(radians)-(sy-cy)*math.sin(radians)
        ry = cy+(sx-cx)*math.sin(radians)+(sy-cy)*math.cos(radians)
        expected = (ox+rx*scale, oy+(float(geometry['height_mm'])-ry)*scale)
        assert (a*u+c*v+e,b*u+d*v+f) == pytest.approx(expected, abs=.001)


def test_font_change_requires_resave_and_published_text_never_renders_again(isolated_drawing_db, monkeypatch):
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        draft = DesignWrite(expected_product_version=1, template_key='liner_v1', parameters={}, print_objects=[text_object()])
        api.save_design(ids[0], draft, db, user)
        stored = json.loads(db.get(DrawingDesign, ids[0]).print_objects_json)[0]
        changed_pixels = {**stored, 'text_rendering': {**stored['text_rendering'], 'sha256': '0'*64}}
        with pytest.raises(HTTPException) as changed_rendering:
            text_service.render_text_artwork(changed_pixels)
        assert changed_rendering.value.status_code == 409 and '重新保存' in changed_rendering.value.detail
        original_hash = stored['text_rendering']['font_sha256']
        loader = text_service._font_source
        def changed_font(*args):
            data, coverage, _ = loader(*args)
            return data, coverage, 'f'*64
        monkeypatch.setattr(text_service, '_font_source', changed_font)
        request = PublishWrite(expected_product_version=1, expected_design_version=1, idempotency_key='text-render-publish-1')
        for action in (lambda: api.preview_design(ids[0], db, user), lambda: api.publish_design(ids[0], request, db, user)):
            with pytest.raises(HTTPException) as changed:
                action()
            assert changed.value.status_code == 409 and '重新保存' in changed.value.detail
        assert db.query(DrawingRelease).count() == 0
        api.save_design(ids[0], draft.model_copy(update={'expected_design_version':1}), db, user)
        stored = json.loads(db.get(DrawingDesign, ids[0], populate_existing=True).print_objects_json)[0]
        assert stored['text_rendering']['font_sha256'] != original_hash
        request.expected_design_version = 2
        published = api.publish_design(ids[0], request, db, user)
        release = db.get(DrawingRelease, published['id'])
        manifest = json.loads(release.manifest_json)
        obj = manifest['print_objects'][0]
        assert obj['kind'] == 'text' and obj['text'] == text_object()['text']
        assert obj['asset_sha256'] == obj['text_rendering']['sha256']
        before_svg = api.release_svg(ids[0], release.id, 'print', db, user).body
        before_pdf = Path(api.download_release(ids[0], release.id, db, user).path).read_bytes()
        def forbidden(*args, **kwargs):
            raise AssertionError('published text was rendered again')
        monkeypatch.setattr(text_service, 'render_text_artwork', forbidden)
        monkeypatch.setattr(api, 'render_text_artwork', forbidden)
        monkeypatch.setattr(api, 'svg', forbidden)
        monkeypatch.setenv('ERP_DRAWING_TEXT_FONT_PATH', 'Z:/missing-font.ttf')
        assert api.release_svg(ids[0], release.id, 'print', db, user).body == before_svg
        assert Path(api.download_release(ids[0], release.id, db, user).path).read_bytes() == before_pdf
        assert api.publish_design(ids[0], request, db, user) == published
        assert base64.b64decode(release_paper_snapshot(release)[1]['svg_urls']['print'].split(',',1)[1]) == before_svg
        asset = resolve_stored_reference(obj['asset_reference'])
        asset.write_bytes(b'corrupt isolated text asset')
        for action in (lambda: api.publish_design(ids[0], request, db, user), lambda: release_paper_snapshot(release)):
            with pytest.raises(HTTPException) as broken:
                action()
            assert broken.value.status_code == 503
        result = audit(Path(engine.url.database), asset.parent.parent)
        assert str(asset.resolve()) in result['damaged']


def test_long_print_details_keep_number_and_revision_on_every_pdf_page():
    geometry = build_geometry('liner_v1', dict(length_mm='100.25', width_mm='80.50'))
    obj = text_object('印刷说明ABCDEFGHIJKLMNOPQRSTUVWXYZ'*6)
    png, _ = text_service.render_text_artwork(obj)
    objects = [dict(obj) for _ in range(30)]
    pdf = engineering_pdf(geometry, customer='隔离长文字', product='分页测试',
        number='TEXT-LONG-001', revision='R07', thickness='3mm', print_objects=objects,
        image_assets={index: (png, 'image/png') for index in range(len(objects))})
    pages = PdfReader(BytesIO(pdf)).pages
    assert len(pages) >= 3
    for page in pages:
        text = page.extract_text()
        assert 'TEXT-LONG-001' in text and 'R07' in text
    details = ''.join(page.extract_text() for page in pages[1:])
    assert ''.join(details.split()).count('角度:0') == 30
