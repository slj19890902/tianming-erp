"""Synthetic-only checks; these are not evidence for the two live customer products."""
import json
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.drawing_design import (DesignWrite, PublishWrite, download_release, get_design,
                                    publish_design, release_svg, save_design)
from app.models import Base
from app.models.customer import Customer
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.production import ProductionTask
from app.models.order import Order, OrderItem
from datetime import date
from app.models.user import User
from app.services.drawing_binding import bind_new_task_drawing, bound_task_release
from app.services.drawing_geometry import build_geometry
from app.services.drawing_geometry import custom_parameter_suggestions
from app.services.secure_uploads import ValidatedUpload, store_private_upload
from scripts.drawing_v2_reconcile import audit


def test_confirmed_620_470_26_28_rule_and_manual_overrides():
    suggested = custom_parameter_suggestions('620', '470', '26')
    assert suggested == dict(top_cover_mm='235', bottom_cover_mm='235', left_fold_mm='26', right_fold_mm='26')
    assert not {'top_fold_mm', 'bottom_fold_mm', 'left_wing_mm', 'right_wing_mm'} & suggested.keys()
    params = dict(panel_width_mm='620', panel_height_mm='470', **suggested,
                  top_fold_mm='28', bottom_fold_mm='28', left_wing_mm='40', right_wing_mm='40')
    geometry = build_geometry('custom_21301634_v1', params)
    assert (geometry['width_mm'], geometry['height_mm']) == ('752', '996')
    modified = build_geometry('custom_21301634_v1', {**params, 'left_wing_mm': '35.5', 'top_cover_mm': '236.25'})
    assert (modified['width_mm'], modified['height_mm']) == ('747.5', '997.25')
    # Reference 308 is a manual sample value, not a rounding rule for width/2.
    assert custom_parameter_suggestions('1165', '615', '25')['top_cover_mm'] == '307.5'


@pytest.mark.parametrize('length,width,height,other_height,wing', [
    ('620', '470', '26', '28', '40'), ('1165', '615', '25', '28', '40')])
def test_custom_four_corners_are_recessed_without_tabs(length,width,height,other_height,wing):
    l,w,h,oh,sw = map(Decimal, (length,width,height,other_height,wing))
    params = dict(panel_width_mm=l,panel_height_mm=w,**custom_parameter_suggestions(l,w,h),
                  top_fold_mm=oh,bottom_fold_mm=oh,left_wing_mm=sw,right_wing_mm=sw)
    geometry = build_geometry('custom_21301634_v1', params)
    points = [(Decimal(part['x1']),Decimal(part['y1'])) for part in geometry['cut']]
    assert len(points) == 12
    x1,x2,y1,y2 = sw+h,sw+h+l,w/2+oh,w/2+oh+w
    # Each of the four inside corners has exactly one horizontal and one
    # vertical cut meeting at the central panel corner (no intervening step).
    for corner in [(x1,y1),(x2,y1),(x1,y2),(x2,y2)]:
        index = points.index(corner)
        previous, following = points[index-1], points[(index+1)%len(points)]
        assert (previous[0] == corner[0]) != (following[0] == corner[0])
        assert (previous[1] == corner[1]) != (following[1] == corner[1])
    area = abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(points,points[1:]+points[:1]))) / 2
    assert area == l * (2*w+2*oh) + 2*(h+sw)*w
    assert len(geometry['score']) == 8


@pytest.fixture
def isolated_drawing_db(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine = create_engine(f"sqlite:///{tmp_path / 'uat.sqlite3'}", connect_args={'timeout': 15})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name='隔离并发验证')
        user = User(username='isolated-publisher', password_hash='x', role='admin', real_name='UAT')
        db.add_all([customer, user])
        db.flush()
        for index in range(4):
            db.add(Product(customer_id=customer.id, product_code=f'UAT-{index}',
                           customer_material_code=f'UAT-{index}', product_name='隔离衬板',
                           length_mm=Decimal('100.25'), width_mm=Decimal('50.5'),
                           height_mm=Decimal('3'), flute_type='B', version=1))
        db.commit()
        ids = [p.id for p in db.query(Product).all()]
        user_id = user.id
    yield engine, ids, user_id
    engine.dispose()


def test_incomplete_reference_draft_and_save_retry(isolated_drawing_db):
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        request = DesignWrite(expected_product_version=1, template_key='custom_21301634_v1',
                              parameters={'top_cover_mm': None}, idempotency_key='partial-save-0001')
        saved = save_design(ids[0], request, db, user)
        assert not saved['geometry_ready']
        assert saved['draft']['parameters']['top_cover_mm'] is None
        assert save_design(ids[0], request, db, user)['draft']['version'] == 1
        with pytest.raises(HTTPException) as changed:
            save_design(ids[0], request.model_copy(update={'parameters': {'top_cover_mm': Decimal('10')}}), db, user)
        assert changed.value.status_code == 409
        with pytest.raises(HTTPException) as incomplete:
            publish_design(ids[0], PublishWrite(expected_product_version=1,
                           expected_design_version=1, idempotency_key='partial-publish-0001'), db, user)
        assert incomplete.value.status_code == 422


def test_parallel_number_allocation_and_same_request(isolated_drawing_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.models.drawing_design import DrawingNumberSequence
    from app.core.time_contract import beijing_today
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        for pid in ids:
            save_design(pid, DesignWrite(expected_product_version=1, template_key='liner_v1', parameters={}), db, user)
        db.add(DrawingNumberSequence(business_date=beijing_today().strftime('%Y%m%d'), last_number=998))
        db.commit()
    barrier = Barrier(4)
    def publish(pid):
        with Session(engine) as db:
            user = db.get(User, uid)
            barrier.wait(timeout=10)
            return publish_design(pid, PublishWrite(expected_product_version=1,
                                  expected_design_version=1, idempotency_key=f'parallel-publish-{pid}'), db, user)
    with ThreadPoolExecutor(max_workers=4) as pool:
        releases = list(pool.map(publish, ids))
    assert len({r['number'] for r in releases}) == 4
    assert {int(r['number'].rsplit('-', 1)[1]) for r in releases} == {999, 1000, 1001, 1002}
    barrier = Barrier(4)
    with ThreadPoolExecutor(max_workers=4) as pool:
        retries = list(pool.map(publish, [ids[0]] * 4))
    assert all(r == releases[0] for r in retries)


def test_storage_failure_does_not_consume_number(isolated_drawing_db, monkeypatch):
    import app.api.drawing_design as api
    from app.models.drawing_design import DrawingRelease, DrawingNumberSequence
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        save_design(ids[0], DesignWrite(expected_product_version=1, template_key='liner_v1', parameters={}), db, user)
        original = api.store_private_upload
        def failure(*args, **kwargs):
            raise OSError('injected unavailable isolated storage')
        monkeypatch.setattr(api, 'store_private_upload', failure)
        payload = PublishWrite(expected_product_version=1, expected_design_version=1, idempotency_key='storage-failure-request')
        with pytest.raises(HTTPException) as error:
            publish_design(ids[0], payload, db, user)
        assert error.value.status_code == 503
        assert db.query(DrawingRelease).count() == db.query(DrawingNumberSequence).count() == 0
        monkeypatch.setattr(api, 'store_private_upload', original)
        assert publish_design(ids[0], payload, db, user)['number'].endswith('-001')


def test_published_svg_is_frozen_and_rule_changes_require_new_draft(isolated_drawing_db, monkeypatch):
    import app.api.drawing_design as api
    from app.models.drawing_design import DrawingRelease
    from app.services.secure_uploads import resolve_stored_reference
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        draft = DesignWrite(expected_product_version=1,template_key='liner_v1',parameters={})
        save_design(ids[0],draft,db,user)
        request = PublishWrite(expected_product_version=1,expected_design_version=1,idempotency_key='frozen-svg-request-1')
        first = publish_design(ids[0],request,db,user)
        original = release_svg(ids[0],first['id'],'structure',db,user).body
        def renderer_must_not_run(*args, **kwargs):
            raise AssertionError('published drawing was regenerated')
        with monkeypatch.context() as scoped:
            scoped.setattr(api,'svg',renderer_must_not_run)
            assert release_svg(ids[0],first['id'],'structure',db,user).body == original
        build = api.build_geometry
        def changed_rule(*args, **kwargs):
            return {**build(*args,**kwargs),'rule_revision':'new-approved-rule'}
        monkeypatch.setattr(api,'build_geometry',changed_rule)
        assert get_design(ids[0],db,user)['publication_outdated']
        # A response retry still denotes its original immutable publication.
        assert publish_design(ids[0],request,db,user) == first
        with pytest.raises(HTTPException) as outdated:
            publish_design(ids[0],request.model_copy(update={'idempotency_key':'frozen-svg-request-2'}),db,user)
        assert outdated.value.status_code == 409
        save_design(ids[0],draft.model_copy(update={'expected_design_version':1}),db,user)
        second = publish_design(ids[0],request.model_copy(update={'expected_design_version':2,'idempotency_key':'frozen-svg-request-3'}),db,user)
        assert second['id'] != first['id'] and second['number'] == first['number']
        assert release_svg(ids[0],first['id'],'structure',db,user).body == original
        manifest = json.loads(db.get(DrawingRelease,second['id']).manifest_json)
        resolve_stored_reference(manifest['svg_snapshots']['structure']['reference']).write_bytes(b'corrupted snapshot')
        with pytest.raises(HTTPException) as corrupted:
            release_svg(ids[0],second['id'],'structure',db,user)
        assert corrupted.value.status_code == 503


@pytest.mark.parametrize('after_commit', [False, True])
def test_commit_response_failure_preserves_only_durable_files(isolated_drawing_db, monkeypatch, after_commit):
    from app.models.drawing_design import DrawingRelease
    from app.services.secure_uploads import resolve_stored_reference
    engine, ids, uid = isolated_drawing_db
    with Session(engine) as db:
        user = db.get(User, uid)
        save_design(ids[0], DesignWrite(expected_product_version=1, template_key='liner_v1', parameters={}), db, user)
        original = db.commit
        def failure():
            if after_commit:
                original()
            raise OSError('injected lost commit response')
        monkeypatch.setattr(db, 'commit', failure)
        payload = PublishWrite(expected_product_version=1, expected_design_version=1,
                               idempotency_key='commit-failure-retry')
        if after_commit:
            result = publish_design(ids[0], payload, db, user)
            release = db.get(DrawingRelease, result['id'])
            assert resolve_stored_reference(release.pdf_reference).is_file()
        else:
            with pytest.raises(HTTPException) as error:
                publish_design(ids[0], payload, db, user)
            assert error.value.status_code == 503
            assert db.query(DrawingRelease).count() == 0
        monkeypatch.setattr(db, 'commit', original)
        result = publish_design(ids[0], payload, db, user)
        assert db.query(DrawingRelease).count() == 1
        assert download_release(ids[0], result['id'], db, user).path


def test_three_templates_and_two_independent_custom_params():
    custom = dict(panel_width_mm='1165', panel_height_mm='615', top_cover_mm='308',
                  bottom_cover_mm='308', top_fold_mm='28', bottom_fold_mm='28',
                  left_fold_mm='25', right_fold_mm='25', left_wing_mm='40', right_wing_mm='40')
    first = build_geometry('custom_21301634_v1', custom)
    assert (first['width_mm'], first['height_mm']) == ('1295', '1287')
    second = build_geometry('custom_21301634_v1', {**custom, 'panel_width_mm': '916',
                             'panel_height_mm': '602', 'top_cover_mm': '200',
                             'bottom_cover_mm': '210', 'left_wing_mm': '35'})
    assert (second['width_mm'], second['height_mm']) != ('1295', '1287')
    assert build_geometry('liner_v1', {'length_mm': '100.25', 'width_mm': '50.5'})['height_mm'] == '100.25'
    assert build_geometry('slotted_v1', dict(panel_1_mm='300', panel_2_mm='200', panel_3_mm='300',
           panel_4_mm='200', body_height_mm='400', top_flap_mm='100', bottom_flap_mm='100',
           glue_flap_mm='30', slot_width_mm='5'))['width_mm'] == '1030'


def test_draft_release_idempotency_scope_and_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine = create_engine(f"sqlite:///{tmp_path / 'uat.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(name='隔离客户甲')
        other = Customer(name='隔离客户乙')
        db.add_all([customer, other])
        db.flush()
        product = Product(customer_id=customer.id, product_code='21301634',
                          customer_material_code='SYNTH-1', product_name='合成验证',
                          length_mm=Decimal('1165'), width_mm=Decimal('615'),
                          height_mm=Decimal('25'), layer_count=7, flute_type='ABC', version=1)
        user = User(username='drawing-v2-uat', password_hash='x', role='admin', real_name='UAT')
        db.add_all([product, user])
        db.commit()
        params = dict(top_cover_mm=Decimal('308'),bottom_cover_mm=Decimal('308'),
                      top_fold_mm=Decimal('28'),bottom_fold_mm=Decimal('28'),
                      left_fold_mm=Decimal('25'),right_fold_mm=Decimal('25'),
                      left_wing_mm=Decimal('40'),right_wing_mm=Decimal('40'))
        save_design(product.id, DesignWrite(expected_product_version=1,
                    template_key='custom_21301634_v1', parameters=params), db, user)
        draft = get_design(product.id, db, user)['draft']
        assert not draft['thickness_approximate'] and draft['thickness_mm'] == '9'
        payload = PublishWrite(expected_product_version=1,
                               expected_design_version=draft['version'],
                               idempotency_key='same-request-key-123')
        first = publish_design(product.id, payload, db, user)
        assert publish_design(product.id, payload, db, user) == first
        assert first['number'].startswith('TM-DWG-')
        assert '1295' in release_svg(product.id, first['id'], 'structure', db, user).body.decode()
        order = Order(order_number='DWG-UAT',customer_id=customer.id,order_date=date(2026,9,21))
        db.add(order)
        db.flush()
        item = OrderItem(order_id=order.id,product_id=product.id,quantity=1,unit_price=0,subtotal=0,
                         snapshot_product_name=product.product_name,snapshot_spec='1165×615×25mm',snapshot_material='UAT')
        db.add(item)
        db.flush()
        task = ProductionTask(order_item_id=item.id)
        db.add(task)
        db.flush()
        bind_new_task_drawing(db, task, product, source_is_new=True)
        db.commit()
        assert bound_task_release(db, task.id).id == first['id']
        limited = User(username='drawing-v2-limited', password_hash='x', role='sales',
                       real_name='limited', customer_access_mode='selected')
        db.add(limited)
        db.commit()
        with pytest.raises(HTTPException) as forbidden:
            release_svg(product.id, first['id'], 'structure', db, limited)
        assert forbidden.value.status_code == 403
        from io import BytesIO
        from PIL import Image
        png = BytesIO()
        Image.new('RGB', (12, 23), 'red').save(png, format='PNG')
        raw = png.getvalue()
        import hashlib
        original = store_private_upload(ValidatedUpload(content=raw, original_filename='customer-logo.png',
                        extension='.png', content_type='image/png', size=len(raw),
                        sha256=hashlib.sha256(raw).hexdigest()), category='product_drawings')
        source = ProductDrawing(product_id=product.id, image_path=original.reference,
                                thumbnail_path=original.reference)
        db.add(source)
        db.commit()
        save_design(product.id, DesignWrite(expected_product_version=1,
                    expected_design_version=1, template_key='custom_21301634_v1', parameters=params,
                    print_objects=[dict(kind='image', source_drawing_id=source.id, panel_id='center', size_confirmed=True,
                                        x_mm=Decimal('7.5'), y_mm=Decimal('7.5'),
                                        width_mm=Decimal('600'), height_mm=Decimal('1150'),
                                        rotation_deg=90)]), db, user)
        second_release = publish_design(product.id, PublishWrite(expected_product_version=1,
                         expected_design_version=2, idempotency_key='image-request-key-456'), db, user)
        assert 'data:image/png;base64,' in release_svg(product.id, second_release['id'], 'print', db, user).body.decode()
        assert audit(tmp_path / 'uat.sqlite3', tmp_path / 'files')['referenced'] == 7
        delivered = download_release(product.id, first['id'], db, user)
        assert delivered.path.suffix == '.pdf'
        from pathlib import Path
        Path(delivered.path).write_bytes(b'broken isolated file')
        with pytest.raises(HTTPException) as missing:
            download_release(product.id, first['id'], db, user)
        assert missing.value.status_code == 503
        assert len(audit(tmp_path / 'uat.sqlite3', tmp_path / 'files')['damaged']) == 1
        product.customer_id = other.id
        db.commit()
        with pytest.raises(HTTPException) as denied:
            release_svg(product.id, first['id'], 'structure', db, user)
        assert denied.value.status_code == 404
