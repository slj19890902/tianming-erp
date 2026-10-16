from copy import deepcopy
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.customer import Customer
from app.models.product import Product
from app.models.mold_tool import MoldTool
from tests.test_mold_tool_workflow import mold_app, _login
from tests.test_p1_103_mold_label_layout import _complete_named_mold
from app.services.customer_label_profile import freeze_product_label_profiles
from app.services import mold_label_layout as layouts


def setup_product(factory, mold_id, customer_code='OTHER'):
    with factory() as db:
        product=db.scalar(select(Product).where(Product.mold_tool_id==mold_id))
        product.customer_drawing_number='DWG-7942511'
        product.customer_model='MODEL-42'
        db.get(Customer, product.customer_id).customer_code=customer_code
        db.commit()
        return product.id, product.version


@pytest.mark.parametrize('code,kind', [('YG','drawing'),('GY','drawing'),('OTHER','name')])
def test_customer_identity_controls_mold_title_and_search(mold_app,code,kind):
    app,factory=mold_app; mid=_complete_named_mold(factory); pid,_=setup_product(factory,mid,code)
    with TestClient(app) as client:
        _login(client,'admin')
        row=client.get(f'/api/warehouse/molds/{mid}/label-preview').json()
        assert row['label_title_kind']==kind
        assert row['label_title_text']==('DWG-7942511' if kind=='drawing' else '布局测试纸箱')
        assert row['label_product_choices'][0]['id']==pid
        found=client.get('/api/warehouse/molds',params={'q':'DWG-7942511'}).json()
        assert [x['id'] for x in found['items']]==[mid]
        for query in ('DWG-7942511','MODEL-42'):
            found=client.get('/api/master/products',params={'product_code':query})
            assert found.status_code==200,found.text
            assert pid in [x['id'] for x in found.json()['items']]


def test_preview_content_freezes_and_same_key_rejects_different_title(mold_app):
    app,factory=mold_app; mid=_complete_named_mold(factory); pid,pv=setup_product(factory,mid)
    with TestClient(app) as client:
        _login(client,'admin')
        body=dict(mold_ids=[mid],source='single',template_version='mold_80x40_v1',idempotency_key='preview-freeze-once',
            content_options=[dict(mold_id=mid,product_id=pid,title_text='短侧板',expected_mold_version=1,expected_product_version=pv)])
        result=client.post('/api/warehouse/molds/label-prints',json=body)
        assert result.status_code==200,result.text
        job=result.json()['print_job_id']
        assert client.post('/api/warehouse/molds/label-prints',json=body).json()['replayed']
        changed=deepcopy(body);changed['content_options'][0]['title_text']='另一个名称'
        assert client.post('/api/warehouse/molds/label-prints',json=changed).status_code==409
        with factory() as db:
            product=db.get(Product,pid);product.product_code='CHANGED-CODE';product.product_name='更改后的主档名称';db.commit()
        frozen=client.get(f'/api/warehouse/molds/{mid}/label',params={'template_version':'mold_80x40_v1','print_job_id':job})
        assert frozen.status_code==200,frozen.text
        row=frozen.json()
        assert row['label_title_text']=='短侧板'
        assert row['label_inventory_codes']==['P1103-BOX-001']
        assert row['selected_product_id']==pid


def test_label_title_save_has_cas_idempotency_and_preserves_product_name(mold_app):
    app,factory=mold_app; mid=_complete_named_mold(factory); pid,_=setup_product(factory,mid)
    with TestClient(app) as client:
        payload=dict(product_id=pid,title_text='底盒',expected_version=1,idempotency_key='save-label-title-once')
        assert client.post(f'/api/warehouse/molds/{mid}/label-title',json=payload).status_code==401
        _login(client,'admin')
        first=client.post(f'/api/warehouse/molds/{mid}/label-title',json=payload)
        assert first.status_code==200,first.text
        assert first.json()['version']==2
        assert client.post(f'/api/warehouse/molds/{mid}/label-title',json=payload).json()==first.json()
        assert client.post(f'/api/warehouse/molds/{mid}/label-title',json={**payload,'idempotency_key':'stale-title-request'}).status_code==409
        row=client.get(f'/api/warehouse/molds/{mid}/label-preview').json()
        assert row['label_title_text']=='底盒'
        with factory() as db: assert db.get(Product,pid).product_name=='布局测试纸箱'


def test_customer_profile_fields_are_frozen_before_hash_and_tail_untouched(mold_app):
    _,factory=mold_app;mid=_complete_named_mold(factory);pid,_=setup_product(factory,mid,'YG')
    with factory() as db:
        plans=[dict(selection_key='test',product_id=pid,customer_id=1)]
        labels=[dict(selection_key='test',quantity=q,label_number=i) for i,q in enumerate((50,20),1)]
        freeze_product_label_profiles(db,plans,labels)
        assert [x['quantity'] for x in labels]==[50,20]
        assert all(x['drawing_number']=='DWG-7942511' for x in labels)
        assert plans[0]['label_profile']=='customer-drawing-code-quantity-v1'
        original=deepcopy(labels)
        product=db.get(Product,pid);product.customer_drawing_number='NEW';db.flush()
        assert labels==original


def test_previous_v12_layout_remains_supported():
    old=layouts._default_layout_v11()
    old['catalog_version']=layouts.V12_CATALOG_VERSION
    for e in old['elements']:
        if e['id']=='customer_name':e.update(x_mm=1.2,y_mm=16.2,width_mm=61.2,height_mm=4.6)
        if e['id']=='inventory_code_side':e.update(x_mm=1.2,y_mm=20.8,width_mm=61.2,height_mm=12.,font_size_mm=12.)
    assert layouts._normalize_snapshot_layout(old)==old
    assert layouts.default_layout()['catalog_version']==layouts.V13_CATALOG_VERSION

from tests.test_supplier_stock_labels_20261016 import stock_replenishment_app, reported, preview, request as label_request


def test_reported_yg_profile_and_drawing_are_in_immutable_job(stock_replenishment_app):
    app,factory=stock_replenishment_app
    with TestClient(app) as client:
        order,item,_=reported(client,factory,23)
        with factory() as db:
            db.get(Customer,1).customer_code='YG'
            db.get(Product,1).customer_drawing_number='YG-DWG-123'
            db.commit()
        package=preview(client,order,item)
        assert [x['quantity'] for x in package['labels']]==[5,5,5,5,3]
        assert all(x['drawing_number']=='YG-DWG-123' for x in package['labels'])
        created=client.post(f'/api/requisition/supplier-orders/{order}/production-packaging-label-jobs',json=label_request(package,'yg-frozen-profile'))
        assert created.status_code==200,created.text
        job=created.json()['job_id']
        confirmed=client.post(f'/api/requisition/production-packaging-label-jobs/{job}/confirm',json=dict(idempotency_key='yg-profile-confirm',confirmed=True))
        assert confirmed.status_code==200,confirmed.text
        with factory() as db:
            p=db.get(Product,1);p.customer_drawing_number='NEW';p.version+=1;db.commit()
        frozen=client.get(f'/api/requisition/production-packaging-label-jobs/{job}')
        assert frozen.status_code==200,frozen.text
        assert frozen.json()['package']['labels'][0]['drawing_number']=='YG-DWG-123'


def test_selected_product_must_be_bound_and_cannot_override_drawing(mold_app):
    app,factory=mold_app;mid=_complete_named_mold(factory);pid,_=setup_product(factory,mid,'GY')
    with TestClient(app) as client:
        _login(client,'admin')
        base=dict(mold_ids=[mid],source='single',template_version='mold_80x40_v1',idempotency_key='invalid-label-content')
        for option in (dict(mold_id=mid,product_id=999999),dict(mold_id=mid,product_id=pid,title_text='伪图号')):
            r=client.post('/api/warehouse/molds/label-prints',json={**base,'content_options':[option]})
            assert r.status_code in (409,422),r.text
        assert client.post(f'/api/warehouse/molds/{mid}/label-title',json=dict(product_id=pid,title_text='不能替换图号',expected_version=1,idempotency_key='drawing-name-reject')).status_code==422


def test_shared_mold_selection_uses_selected_customer_and_product(mold_app):
    app,factory=mold_app;mid=_complete_named_mold(factory);setup_product(factory,mid)
    with factory() as db:
        customer=Customer(customer_number=9918,customer_code='GY',name='共享光洋',chinese_short_name='光洋',payment_term_days=30)
        db.add(customer);db.flush()
        product=Product(customer_id=customer.id,product_code='80012345',customer_material_code='80012345',product_name='共享子件',customer_drawing_number='GY-DWG-2',mold_tool_id=mid,report_length_mm=880,report_width_mm=425)
        db.add(product);db.commit();pid=product.id
    with TestClient(app) as client:
        _login(client,'admin')
        row=client.get(f'/api/warehouse/molds/{mid}/label-preview').json()
        assert len(row['label_product_choices'])==2
        made=client.post('/api/warehouse/molds/label-prints',json=dict(mold_ids=[mid],source='single',template_version='mold_80x40_v1',idempotency_key='shared-selected-product',content_options=[dict(mold_id=mid,product_id=pid)]))
        assert made.status_code==200,made.text
        frozen=client.get(f'/api/warehouse/molds/{mid}/label',params=dict(template_version='mold_80x40_v1',print_job_id=made.json()['print_job_id'])).json()
        assert frozen['label_title_kind']=='drawing' and frozen['label_title_text']=='GY-DWG-2'
        assert frozen['label_customer_name']=='光洋' and frozen['label_inventory_codes']==['80012345']
        assert frozen['qr_data_url']==row['qr_data_url']


def test_title_audit_failure_rolls_back_version_and_content(mold_app,monkeypatch):
    from app.api import warehouse
    app,factory=mold_app;mid=_complete_named_mold(factory);pid,_=setup_product(factory,mid)
    def fail(*args,**kwargs):raise RuntimeError('audit failure')
    monkeypatch.setattr(warehouse,'append_audit_event',fail)
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client,'admin')
        r=client.post(f'/api/warehouse/molds/{mid}/label-title',json=dict(product_id=pid,title_text='不能提交',expected_version=1,idempotency_key='atomic-title-failure'))
        assert r.status_code==500
    with factory() as db:
        row=db.get(MoldTool,mid)
        assert row.version==1 and row.label_overrides_json is None


def test_unified_pinyin_tokens_preserve_narrowing_and_scope(mold_app):
    app,factory=mold_app;mid=_complete_named_mold(factory);setup_product(factory,mid)
    with TestClient(app) as client:
        _login(client,'admin')
        mapping=json.dumps({'yan': [1]})
        response=client.get('/api/warehouse/molds',params={'q':'yan DWG-7942511','keyword_customers':mapping})
        assert response.status_code==200,response.text
        assert [row['id'] for row in response.json()['items']]==[mid]
        # A customer expansion belongs to its own token; it cannot bypass
        # the second token and return every mold of that customer.
        assert client.get('/api/warehouse/molds',params={'q':'yan DOES-NOT-EXIST','keyword_customers':mapping}).json()['items']==[]
        assert client.get('/api/warehouse/molds',params={'q':'yan','keyword_customers':'{"other":[1]}'}).status_code==422
        assert client.get('/api/warehouse/molds',params={'q':'yan','keyword_customers':'{"yan":[true]}'}).status_code==422
    from app.models.user import User
    from app.api.warehouse import _mold_tools_query
    with factory() as db:
        user=db.scalar(select(User).where(User.username=='sales'))
        user.customer_access_mode='selected';db.flush()
        query,scope=_mold_tools_query(db=db,user=user,q='yan',include_inactive=False,keyword_customer_ids={'yan':{1}})
        assert scope==set()
        assert db.scalars(query).all()==[]
