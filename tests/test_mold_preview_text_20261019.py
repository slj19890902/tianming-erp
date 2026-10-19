from copy import deepcopy
import json
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from tests.test_mold_tool_workflow import mold_app, _login
from tests.test_p1_103_mold_label_layout import _complete_named_mold
from app.services.mold_label_content import (with_label_texts,label_text_overrides,
    canonical_label_overrides,with_product_label_title,MoldLabelContentError)


def test_label_text_scopes_blank_reset_and_old_form_preservation():
    value=with_label_texts(None,{'inventory_code_side':'21301834','customer_name':'天华'})
    value=with_label_texts(value,{'drawing_number':'','inventory_code_side':'834'},8)
    assert label_text_overrides(value,8)=={'inventory_code_side':'834','customer_name':'天华','drawing_number':''}
    assert label_text_overrides(value,9)=={'inventory_code_side':'21301834','customer_name':'天华'}
    value=with_product_label_title(value,8,'短名称')
    value=canonical_label_overrides({'remarks':'原模具表单编辑'},preserve_json=value)
    assert label_text_overrides(value,8)['drawing_number']==''
    assert label_text_overrides(with_label_texts(value,{},8),8)['inventory_code_side']=='21301834'
    for texts in ({'mold_qr':'fake'},{'inventory_code_side':'x'*501},{'customer_name':'bad\x00'}):
        with pytest.raises(MoldLabelContentError):with_label_texts(value,texts)


def test_text_save_replay_future_label_and_original_master_unchanged(mold_app):
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    app,factory=mold_app;mid=_complete_named_mold(factory)
    with factory() as db:
        mold=db.get(MoldTool,mid);product=db.scalar(select(Product).where(Product.mold_tool_id==mid))
        before=(product.product_code,product.product_name,product.customer_drawing_number,product.report_length_mm,mold.rack_location)
        pid,version,pversion=product.id,mold.version,product.version
    payload={'idempotency_key':'preview-all-text-replay-1','items':[{'mold_id':mid,'product_id':pid,
        'expected_version':version,'expected_product_version':pversion,
        'texts':{'inventory_code_side':'834','customer_name':'天华','drawing_number':'','rack_location':'R01-2-1','cutting_mode':'4模','report_specification':'自定尺寸'}}]}
    with TestClient(app) as client:
        _login(client,'admin')
        saved=client.post('/api/warehouse/molds/label-texts',json=payload)
        assert saved.status_code==200,saved.text
        replay=client.post('/api/warehouse/molds/label-texts',json=payload)
        assert replay.status_code==200,replay.text
        current=client.get(f'/api/warehouse/molds/{mid}/label-preview')
        assert current.status_code==200,current.text
        assert current.json()['label_text_overrides']==payload['items'][0]['texts']
        conflict=deepcopy(payload);conflict['items'][0]['texts']['inventory_code_side']='other'
        assert client.post('/api/warehouse/molds/label-texts',json=conflict).status_code==409
        stale=deepcopy(payload);stale['idempotency_key']='preview-all-text-stale-1'
        assert client.post('/api/warehouse/molds/label-texts',json=stale).status_code==409
    with factory() as db:
        mold=db.get(MoldTool,mid);product=db.get(Product,pid)
        assert mold.version==version+1
        assert (product.product_code,product.product_name,product.customer_drawing_number,product.report_length_mm,mold.rack_location)==before


def test_history_without_text_map_does_not_inherit_new_master_default():
    from app.services.mold_label_preview import apply_frozen_content,freeze_content,normalize_content_snapshots,CONTENT_FIELDS
    row={key:'' for key in CONTENT_FIELDS};row.update(mold_id=1,selected_product_id=None,label_title_kind='drawing',label_inventory_codes=['21301834'],label_text_overrides={'inventory_code_side':'834'})
    content=freeze_content(deepcopy(row),{})
    assert normalize_content_snapshots([content])[0]['label_text_overrides']['inventory_code_side']=='834'
    old={k:v for k,v in content.items() if k!='label_text_overrides'}
    original=deepcopy(old)
    assert apply_frozen_content(deepcopy(row),{'layout':{'content_snapshots':[old]}})['label_text_overrides']=={}
    assert old==original
    assert apply_frozen_content(deepcopy(row),{'layout':{'content_snapshots':[content]}})['label_text_overrides']['inventory_code_side']=='834'


@pytest.mark.parametrize('rack',['R01','R02','R03','R04'])
def test_compact_positions_preserve_stable_cell_id_and_stored_alias(rack):
    from app.services.mold_cells import projected_cells
    from app.services.mold_location import current_mold_cell_alias_codes
    source={'mold_rack_code':rack,'mold_cells':[{'id':'00000000-0000-0000-0000-000000000001','alias':'A1','level':2,'grid':1}]}
    before=deepcopy(source);c=projected_cells(source)[0]
    assert c['alias']==c['short_label']==f'{rack}-2-1'
    assert c['stored_alias']=='A1' and c['location_code']=='MCELL-00000000-0000-0000-0000-000000000001'
    assert source==before
    floors=[{'racks':[source]}]
    assert current_mold_cell_alias_codes('A1',floors=floors)==[c['location_code']]
    assert current_mold_cell_alias_codes(f'{rack}-2-1',floors=floors)==[c['location_code']]


def test_text_reader_contract_requires_saved_facts_but_allows_old_reader_before_save(tmp_path):
    import sqlite3
    from desktop_assistant.reader_contract import target_readers, MOLD_LABEL_TEXT_READER as cap
    path=tmp_path/'readers.sqlite3'
    with sqlite3.connect(path) as db:db.execute('create table mold_tools(label_overrides_json text)')
    source={'reader_capabilities':{cap:1}}
    target_readers(path,source,{'reader_capabilities':{}},None,None,factory=False)
    with sqlite3.connect(path) as db:db.execute('insert into mold_tools values(?)',(with_label_texts(None,{'inventory_code_side':'834'}),))
    with pytest.raises(ValueError):target_readers(path,source,{'reader_capabilities':{}},None,None,factory=False)
    target_readers(path,source,source,None,None,factory=False)
    with pytest.raises(ValueError):target_readers(path,{'reader_capabilities':{cap:1,'unknown_future':1}},source,None,None,factory=False)


def test_batch_text_save_is_atomic_on_stale_second_mold_and_rejects_restricted_scope(mold_app):
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    app,factory=mold_app
    first=_complete_named_mold(factory)
    second=_complete_named_mold(factory,mold_code='TEXT-SECOND',product_code='TEXT-P2')
    with factory() as db:
        a=db.get(MoldTool,first);b=db.get(MoldTool,second);before=a.version
        items=[{'mold_id':first,'expected_version':a.version,'texts':{'customer_name':'改变'}},
               {'mold_id':second,'expected_version':b.version+1,'texts':{'customer_name':'失败'}}]
        sales=db.scalar(select(User).where(User.username=='sales'));sales.customer_access_mode='selected'
        db.add(UserCustomerScope(user_id=sales.id,customer_id=1));db.commit()
    body={'items':items,'idempotency_key':'atomic-label-text-00001'}
    with TestClient(app) as client:
        _login(client,'sales')
        assert client.post('/api/warehouse/molds/label-texts',json=body).status_code==403
        _login(client,'admin')
        assert client.post('/api/warehouse/molds/label-texts',json=body).status_code==409
    with factory() as db:
        assert db.get(MoldTool,first).version==before
        assert label_text_overrides(db.get(MoldTool,first).label_overrides_json)=={}


def test_saved_texts_create_new_frozen_print_without_rewriting_history(mold_app):
    from app.models.mold_tool import MoldTool
    app,factory=mold_app;mid=_complete_named_mold(factory)
    with TestClient(app) as client:
        _login(client,'admin')
        def job(key):
            r=client.post('/api/warehouse/molds/label-prints',json=dict(mold_ids=[mid],source='single',template_version='mold_80x40_v1',idempotency_key=key))
            assert r.status_code==200,r.text
            return r.json()['print_job_id']
        old=job('text-history-before-00001')
        url=f'/api/warehouse/molds/{mid}/label'
        params={'template_version':'mold_80x40_v1','print_job_id':old}
        before=client.get(url,params=params).json()
        with factory() as db:version=db.get(MoldTool,mid).version
        saved=client.post('/api/warehouse/molds/label-texts',json={'items':[dict(mold_id=mid,expected_version=version,texts={'inventory_code_side':'834','drawing_number':''})],'idempotency_key':'text-history-save-00001'})
        assert saved.status_code==200,saved.text
        current=job('text-history-after-00001')
        after=client.get(url,params={**params,'print_job_id':current})
        assert after.status_code==200,after.text
        assert after.json()['label_text_overrides']=={'inventory_code_side':'834','drawing_number':''}
        historical=client.get(url,params=params).json()
        assert historical['label_text_overrides']==before.get('label_text_overrides',{})=={}
        assert historical['label_layout']==before['label_layout']


def test_text_save_audit_failure_rolls_back_and_stale_product_is_rejected(mold_app,monkeypatch):
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.api import warehouse
    app,factory=mold_app;mid=_complete_named_mold(factory)
    with factory() as db:
        mold=db.get(MoldTool,mid);p=db.scalar(select(Product).where(Product.mold_tool_id==mid))
        version,pid,pversion=mold.version,p.id,p.version
    body={'items':[dict(mold_id=mid,expected_version=version,product_id=pid,expected_product_version=pversion+1,texts={'inventory_code_side':'834'})],'idempotency_key':'text-stale-product-00001'}
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client,'admin')
        assert client.post('/api/warehouse/molds/label-texts',json=body).status_code==409
        body['items'][0]['expected_product_version']=pversion
        body['idempotency_key']='text-audit-failure-00001'
        def broken(*a,**kw):raise RuntimeError('audit unavailable')
        monkeypatch.setattr(warehouse,'append_audit_event',broken)
        assert client.post('/api/warehouse/molds/label-texts',json=body).status_code==500
    with factory() as db:
        mold=db.get(MoldTool,mid)
        assert mold.version==version and label_text_overrides(mold.label_overrides_json,pid)=={}


def test_same_schema_old_reader_rejected_after_label_text_use(tmp_path,monkeypatch):
    import sqlite3
    from tests.test_order_inventory_reader_contract import fixture_manager
    from desktop_assistant.reader_contract import MOLD_LABEL_TEXT_READER as cap
    manager=fixture_manager(tmp_path,monkeypatch,activation=False)
    database=tmp_path/'shared/data/carton_erp.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('create table mold_tools(label_overrides_json text)')
        db.execute('insert into mold_tools values(?)',(with_label_texts(None,{'drawing_number':'短标题'}),))
    assert not manager.compatible('new','eg1008sc')
    monkeypatch.setattr(manager,'_shared_finished_reader',lambda release,capability='':capability==cap)
    assert manager.compatible('new','eg1008sc')


def test_customer_drawing_label_edits_do_not_change_another_bound_product(mold_app):
    from app.models.product import Product
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool
    app,factory=mold_app;mid=_complete_named_mold(factory)
    with factory() as db:
        customer=db.get(Customer,1);customer.customer_code='YG'
        first=db.scalar(select(Product).where(Product.mold_tool_id==mid))
        second=Product(customer_id=first.customer_id,product_code='SECOND',customer_material_code='SECOND',product_name='另一产品',box_category='normal',production_process='模切',mold_tool_id=mid)
        db.add(second);db.commit()
        ids=[(first.id,first.version),(second.id,second.version)]
        version=db.get(MoldTool,mid).version
    with TestClient(app) as client:
        _login(client,'admin')
        for index,(pid,pversion) in enumerate(ids):
            response=client.post('/api/warehouse/molds/label-texts',json={'items':[dict(mold_id=mid,product_id=pid,expected_product_version=pversion,expected_version=version,texts={'drawing_number':str(index),'inventory_code_side':f'编码{index}'})],'idempotency_key':f'bound-product-label-{index}'})
            assert response.status_code==200,response.text
            assert response.headers['cache-control']=='private, no-store, max-age=0'
            version=response.json()['items'][0]['version']
    with factory() as db:
        data=db.get(MoldTool,mid).label_overrides_json
        assert label_text_overrides(data,ids[0][0])['drawing_number']=='0'
        assert label_text_overrides(data,ids[1][0])['drawing_number']=='1'


def test_renderer_edits_escape_text_preserve_blank_and_physical_geometry():
    from pathlib import Path
    import subprocess
    from app.services.mold_label_layout import default_layout
    script=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const window={};vm.runInNewContext(fs.readFileSync('static/assets/mold-label-layout.js','utf8'),{window});
const layout=JSON.parse(fs.readFileSync(0,'utf8'));
const row={mold_id:1,qr_data_url:'data:image/png;base64,fixture',label_inventory_code:'21301834',label_title_kind:'drawing',label_title_text:'old',label_rack_location:'1F-M-R01-L2-G01',label_text_overrides:{drawing_number:'自定标题<&>',inventory_code_side:'1834',customer_name:''}};
const html=window.TmMoldLabelLayout.labelHtml(row,{version:0,layout});
assert.ok(html.includes('自定标题&lt;&amp;&gt;'));assert.ok(!html.includes('图号自定'));
assert.match(html,/data-layout-id="customer_name"[^>]*><\/div>/);
assert.match(html,/data-layout-id="inventory_code_side"[^>]*font-size:12mm[^>]*>1834<\/div>/);
assert.match(html,/data-layout-id="mold_qr"[^>]*width:15mm;height:15mm/);
assert.ok(html.includes('R01-2-1'));assert.ok(!html.includes('位置 R01'));
assert.ok(window.TmMoldLabelLayout.labelHtml({...row,label_rack_location:'1F-M-R01-G02'},{version:0,layout}).includes('1F-M-R01-G02'));
assert.ok(window.TmMoldLabelLayout.labelHtml({...row,label_rack_location:'1F-M-R04-L02-G001'},{version:0,layout}).includes('R04-2-1'));
'''
    result=subprocess.run(['node','-e',script],input=json.dumps(default_layout()),text=True,capture_output=True,cwd=Path(__file__).parents[1])
    assert result.returncode==0,result.stderr


def test_frozen_print_requires_new_reader_even_after_master_reset(tmp_path):
    import sqlite3
    from desktop_assistant.reader_contract import target_readers,MOLD_LABEL_TEXT_READER as cap
    path=tmp_path/'history-only.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table mold_label_print_jobs(label_layout_payload_json text)')
        db.execute('insert into mold_label_print_jobs values(?)',(json.dumps({'content_snapshots':[{'mold_id':1,'label_text_overrides':{'inventory_code_side':'834'}}]}),))
    with pytest.raises(ValueError):target_readers(path,{}, {},None,None,factory=False)
    target_readers(path,{}, {'reader_capabilities':{cap:1}},None,None,factory=False)
