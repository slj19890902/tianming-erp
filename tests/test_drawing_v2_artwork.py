"""Isolated original preservation, bounds, physical-size and publication checks."""
import hashlib
import json
from io import BytesIO

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw
from sqlalchemy.orm import Session

from app.api.drawing_design import DesignWrite, PublishWrite, publish_design, save_design, release_svg
from app.models.drawing_design import DrawingRelease
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services.drawing_artwork import inspect_artwork, prepare_artwork
from app.services.product_drawings import save_product_drawing_files
from app.services.secure_uploads import ValidatedUpload, resolve_stored_reference
from test_drawing_v2_isolated import isolated_drawing_db


def artwork():
    image = Image.new('RGBA', (200, 100), (255, 255, 255, 0))
    ImageDraw.Draw(image).rectangle((50, 25, 149, 74), fill='black')
    stream = BytesIO()
    image.save(stream, 'PNG')
    content = stream.getvalue()
    return ValidatedUpload(content=content, original_filename='customer.png', extension='.png',
        content_type='image/png', size=len(content), sha256=hashlib.sha256(content).hexdigest())


def test_preserve_original_and_explicit_alpha_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path))
    upload = artwork()
    saved = save_product_drawing_files(product_id=1, upload=upload, preserve_original=True)
    assert resolve_stored_reference(saved.image_path).read_bytes() == upload.content
    assert inspect_artwork(upload.content)['alpha_bounds_pixels'] == [50, 25, 150, 75]
    with Image.open(resolve_stored_reference(saved.thumbnail_path)) as thumb:
        assert thumb.getpixel((0, 0))[3] == 0
    processed, _, _, _ = prepare_artwork(upload.content, dict(image_bounds='alpha',width_mm='40',height_mm='20'))
    with Image.open(BytesIO(processed)) as image:
        assert image.size == (100, 50)
    white = BytesIO()
    Image.new('RGB',(80,60),'white').save(white,'PNG')
    assert inspect_artwork(white.getvalue())['alpha_bounds_pixels'] == [0,0,80,60]
    with pytest.raises(HTTPException, match='') as distorted:
        prepare_artwork(upload.content,dict(width_mm=40,height_mm=30))
    assert distorted.value.status_code == 422


def test_exif_orientation_and_pdf_transparency(tmp_path,monkeypatch):
    from app.services.drawing_exports import engineering_pdf
    from app.services.drawing_geometry import build_geometry
    from pypdf import PdfReader
    buffer = BytesIO()
    exif = Image.Exif(); exif[274] = 6
    Image.new('RGB',(30,10),'red').save(buffer,'JPEG',exif=exif)
    raw=buffer.getvalue()
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR',str(tmp_path))
    upload=ValidatedUpload(content=raw,original_filename='rotated.jpg',extension='.jpg',
        content_type='image/jpeg',size=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    saved=save_product_drawing_files(product_id=1,upload=upload,preserve_original=True)
    with Image.open(resolve_stored_reference(saved.thumbnail_path)) as thumb:
        assert thumb.size==(10,30)
    assert resolve_stored_reference(saved.image_path).read_bytes()==raw
    content, _, _, info = prepare_artwork(buffer.getvalue(),dict(width_mm=10,height_mm=30))
    assert info['canvas_pixels'] == [10,30] and info['orientation_normalized']
    with Image.open(BytesIO(content)) as normalized:
        assert normalized.size == (10,30)
    obj = dict(kind='image',panel_id='face',width_mm=40,height_mm=20,x_mm=5,y_mm=5,rotation_deg=0)
    geometry = build_geometry('liner_v1',dict(length_mm=100,width_mm=80))
    pdf = engineering_pdf(geometry,customer='隔离',product='透明验证',number='UAT',revision='R00',
                          thickness='3mm',print_objects=[obj],image_assets={0:(artwork().content,'image/png')})
    reader = PdfReader(BytesIO(pdf))
    images = reader.pages[0]['/Resources']['/XObject'].get_object()
    assert any('/SMask' in image.get_object() for image in images.values())


def test_expanded_exif_image_is_rejected_before_publication():
    import random
    buffer=BytesIO()
    exif=Image.Exif(); exif[274]=6
    Image.frombytes('RGB',(2048,2048),random.Random(12).randbytes(2048*2048*3)).save(buffer,'JPEG',quality=85,exif=exif)
    assert len(buffer.getvalue()) < 5_000_000
    with pytest.raises(HTTPException) as expanded:
        prepare_artwork(buffer.getvalue(),dict(width_mm=100,height_mm=100))
    assert expanded.value.status_code == 422 and '处理稿超过5MB' in expanded.value.detail


def test_disable_new_work_retains_published_download(isolated_drawing_db,monkeypatch):
    from app.api.drawing_design import get_design, download_release
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user=db.get(User,uid)
        draft=DesignWrite(expected_product_version=1,template_key='liner_v1',parameters={})
        save_design(ids[0],draft,db,user)
        release=publish_design(ids[0],PublishWrite(expected_product_version=1,expected_design_version=1,
                                                 idempotency_key='switch-existing-release'),db,user)
        monkeypatch.setenv('ERP_DRAWING_V2_ENABLED','0')
        assert not get_design(ids[0],db,user)['editing_enabled']
        assert download_release(ids[0],release['id'],db,user).path.is_file()
        with pytest.raises(HTTPException) as disabled:
            save_design(ids[0],draft,db,user)
        assert disabled.value.status_code == 503


def test_unconfirmed_photo_cannot_publish_and_frozen_original_is_verified(isolated_drawing_db):
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User,uid)
        saved = save_product_drawing_files(product_id=ids[0],upload=artwork(),preserve_original=True)
        source = ProductDrawing(product_id=ids[0],image_path=saved.image_path,thumbnail_path=saved.thumbnail_path)
        db.add(source); db.commit()
        obj = dict(kind='image',source_drawing_id=source.id,panel_id='face',width_mm=40,height_mm=20,
                   x_mm=2,y_mm=2,image_bounds='alpha')
        draft = DesignWrite(expected_product_version=1,template_key='liner_v1',parameters={},print_objects=[obj])
        save_design(ids[0],draft,db,user)
        request = PublishWrite(expected_product_version=1,expected_design_version=1,idempotency_key='artwork-publish-1')
        with pytest.raises(HTTPException) as scale:
            publish_design(ids[0],request,db,user)
        assert scale.value.status_code == 422
        assert db.query(DrawingRelease).count() == 0
        obj['size_confirmed'] = True
        save_design(ids[0],DesignWrite(**{**draft.model_dump(), 'expected_design_version':1,'print_objects':[obj]}),db,user)
        request.expected_design_version = 2
        release = publish_design(ids[0],request,db,user)
        manifest = json.loads(db.get(DrawingRelease,release['id']).manifest_json)
        frozen = manifest['print_objects'][0]
        assert frozen['source_original_preserved']
        assert resolve_stored_reference(frozen['original_reference']).read_bytes() == artwork().content
        before = release_svg(ids[0],release['id'],'print',db,user).body
        resolve_stored_reference(saved.image_path).write_bytes(b'changed current source')
        assert publish_design(ids[0],request,db,user) == release
        assert release_svg(ids[0],release['id'],'print',db,user).body == before
        resolve_stored_reference(frozen['original_reference']).write_bytes(b'corrupt frozen original')
        with pytest.raises(HTTPException) as damaged:
            publish_design(ids[0],request,db,user)
        assert damaged.value.status_code == 503


def test_customer_number_added_with_same_revision_keeps_history(isolated_drawing_db):
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User,uid)
        draft = DesignWrite(expected_product_version=1,template_key='liner_v1',parameters={})
        save_design(ids[0],draft,db,user)
        request = PublishWrite(expected_product_version=1,expected_design_version=1,idempotency_key='customer-number-1')
        original = publish_design(ids[0],request,db,user)
        assert original['revision'] == 'R00'
        draft.expected_design_version=1
        draft.customer_number=' 客户-001 '
        draft.customer_revision='R00'
        save_design(ids[0],draft,db,user)
        second = publish_design(ids[0],PublishWrite(expected_product_version=1,expected_design_version=2,
                                idempotency_key='customer-number-2'),db,user)
        assert second['number'] == ' 客户-001 ' and second['revision'] == 'R00'
        assert publish_design(ids[0],request,db,user) == original
        draft.expected_design_version=2
        save_design(ids[0],draft,db,user)
        with pytest.raises(HTTPException) as duplicate:
            publish_design(ids[0],PublishWrite(expected_product_version=1,expected_design_version=3,
                           idempotency_key='customer-number-3'),db,user)
        assert duplicate.value.status_code == 409 and '该图号和版次' in duplicate.value.detail
        assert db.query(DrawingRelease).count() == 2


@pytest.mark.parametrize('after_commit',[False,True])
def test_original_upload_commit_failure_preserves_only_durable_files(isolated_drawing_db,monkeypatch,after_commit):
    import asyncio
    from fastapi import UploadFile
    from starlette.datastructures import Headers
    from sqlalchemy.exc import SQLAlchemyError
    from app.api.products import _create_drawing_version
    from app.services.secure_uploads import private_upload_root
    engine, ids, uid=isolated_drawing_db
    with Session(engine) as db:
        user=db.get(User,uid)
        commit=db.commit
        def lost_response():
            if after_commit:
                commit()
            raise SQLAlchemyError('injected isolated upload response failure')
        monkeypatch.setattr(db,'commit',lost_response)
        file=UploadFile(file=BytesIO(artwork().content),filename='source.png',headers=Headers({'content-type':'image/png'}))
        if after_commit:
            drawing=asyncio.run(_create_drawing_version(ids[0],file,db,user,preserve_original=True))
            assert resolve_stored_reference(drawing.image_path).read_bytes() == artwork().content
            assert db.query(ProductDrawing).count() == 1
        else:
            with pytest.raises(SQLAlchemyError):
                asyncio.run(_create_drawing_version(ids[0],file,db,user,preserve_original=True))
            assert db.query(ProductDrawing).count() == 0
            assert not list((private_upload_root()/'drawing_originals').glob('*.png'))
