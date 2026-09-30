from types import SimpleNamespace
import json
import pytest
from fastapi.testclient import TestClient
from app.models.product import Product
from app.services.drawing_paper import flute_paper
from tests.test_drawing_workbench_http import _fixture, _app


@pytest.mark.parametrize('flute,mm', [('AB',7),('B',3),('A',4),('E',1),('白卡',1),('BE',4),('ABC',9),('AAA',12),(' AB瓦 ',7),('NONE',1)])
def test_flute_thickness_not_material_or_layer_guess(flute, mm):
    for material in (None, 1, 999):
        p=SimpleNamespace(flute_type=flute, layer_count=1 if flute=='NONE' else 7, material_id=material)
        assert flute_paper(p)['thickness_mm']==mm


def test_flute_slot_preview_save_publish_failure_and_frozen_release(tmp_path, monkeypatch):
    engine, factory, ids=_fixture(tmp_path)
    with factory() as db:
        p=db.get(Product,ids['one']);p.box_style='隔板';p.flute_type='AB';db.commit()
    root=f"/api/master/products/{ids['one']}/managed-drawing"
    params=dict(length_mm=290,height_mm=233,slot_count=2,slot_width_mm=99,slot_depth_mm=100,slot_pitch_mm=100,slot_offset_mm=70,slot_edge=0)
    state={'slot_width_mode':'flute'}
    try:
        with TestClient(_app(factory,[ids['admin']])) as c:
            context=c.get(root+'/workbench-context').json()
            assert context['template_defaults']['partition_v1']['slot_width_mm']==7
            preview=c.post(root+'/workbench-preview',json={'template_key':'partition_v1','parameters':params,'editor_state':state})
            assert preview.status_code==200,preview.text
            assert preview.json()['geometry']['dimensions']['槽宽']=='7'
            data=dict(expected_product_version=1,template_key='partition_v1',parameters=params,editor_state=state,idempotency_key='two-d-save-0001')
            first=c.put(root,json=data);assert first.status_code==200,first.text
            assert first.json()['draft']['thickness_mm']=='7'
            assert first.json()['draft']['parameters']['slot_width_mm']=='7'
            monkeypatch.delenv('ERP_FILE_STORAGE_DIR',raising=False)
            release=dict(expected_product_version=1,expected_design_version=1,idempotency_key='two-d-publish-0001')
            assert c.post(root+'/releases',json=release).status_code==503
            assert c.get(root).json()['draft']['version']==1
            second=c.put(root,json={**data,'expected_design_version':1,'idempotency_key':'two-d-save-0002'})
            assert second.status_code==200,second.text
            monkeypatch.setenv('ERP_FILE_STORAGE_DIR',str(tmp_path/'files'))
            release.update(expected_design_version=2,idempotency_key='two-d-publish-0002')
            published=c.post(root+'/releases',json=release);assert published.status_code==200,published.text
            assert c.post(root+'/releases',json=release).json()==published.json()
            with factory() as db:
                from app.models.drawing_design import DrawingRelease
                frozen=db.get(DrawingRelease,published.json()['id']).manifest_json
                p=db.get(Product,ids['one']);p.flute_type='AAA';p.version+=1;db.commit()
                assert db.get(DrawingRelease,published.json()['id']).manifest_json==frozen
                assert json.loads(frozen)['geometry']['dimensions']['槽宽']=='7'
            assert c.put(root,json={**data,'expected_design_version':2,'idempotency_key':'two-d-save-stale'}).status_code==409
    finally: engine.dispose()


def test_managed_storage_uses_verified_shared_root(tmp_path,monkeypatch):
    from desktop_assistant.server_entry import configure_managed_drawing_storage
    control=tmp_path/'install/control';control.mkdir(parents=True)
    monkeypatch.delenv('ERP_FILE_STORAGE_DIR',raising=False)
    monkeypatch.setenv('ERP_DATABASE_PATH',str(tmp_path/'unrelated.sqlite3'))
    with pytest.raises(ValueError): configure_managed_drawing_storage(control)
    monkeypatch.setenv('ERP_DATABASE_PATH',str(control.parent/'shared/data/carton_erp.sqlite3'))
    configure_managed_drawing_storage(control)
    import os
    assert os.environ['ERP_FILE_STORAGE_DIR']==str((control.parent/'shared/data/private_uploads').resolve())
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR',str(tmp_path/'explicit-files'))
    configure_managed_drawing_storage(control)
    assert os.environ['ERP_FILE_STORAGE_DIR']==str(tmp_path/'explicit-files')
