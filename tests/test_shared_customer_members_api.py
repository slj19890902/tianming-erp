from fastapi.testclient import TestClient
from app.models.product import Product
from app.models.shared_finished_stock import SharedFinishedGroup,SharedFinishedPolicy
from tests.test_shared_finished_stock_api import shared_api
from tests.test_p1_18_pdf_inventory_contract import inventory_preview_app,_login

PREFIX='/api/warehouse/finished/shared-stock'


def test_actual_customer_picker_and_member_state_api_preserve_scope(shared_api):
    app,factory,ids=shared_api
    with factory() as db:
        source=db.get(Product,ids['shared_product']);code=source.product_code
        target=db.get(Product,ids['target']);target.product_code=code;db.commit()
    with TestClient(app) as client:
        _login(client,'preview-other-customer')
        assert client.get(PREFIX+f"/products/{ids['shared_product']}/customers").status_code==403
        _login(client,'shared-admin')
        search=client.get(PREFIX+'/products',params={'keyword':code});assert search.status_code==200,search.text
        assert any(p['id']==ids['target'] and p['group_id'] is None for p in search.json()['items'])
        choices=client.get(PREFIX+f"/products/{ids['shared_product']}/customers");assert choices.status_code==200,choices.text
        assert any(any(p['id']==ids['target'] and p['reason'] is None for p in c['products']) for c in choices.json()['items'])
        request=dict(product_ids=[ids['shared_product'],ids['target']],lot_ids=[ids['shared_lot']],auto_enroll=True)
        preview=client.post(PREFIX+'/preview',json=request);assert preview.status_code==200,preview.text
        saved=client.post(PREFIX+'/confirm',json={**request,'preview_hash':preview.json()['preview_hash'],
            'operation_key':'dept-create','evidence':'虚构部门核实','physical_match_confirmed':True})
        assert saved.status_code==200,saved.text
        gid=saved.json()['group_id']
        with factory() as db:assert db.get(SharedFinishedPolicy,gid).auto_enroll is True
        args=dict(action='members',expected_version=1,product_ids=request['product_ids'],disabled_product_ids=[ids['target']])
        preview=client.post(PREFIX+f'/groups/{gid}/preview',json=args);assert preview.status_code==200,preview.text
        result=client.post(PREFIX+f'/groups/{gid}/confirm',json={**args,'preview_hash':preview.json()['preview_hash'],
            'operation_key':'dept-pause','evidence':'虚构部门暂停','physical_match_confirmed':True})
        assert result.status_code==200,result.text
        detail=client.get(PREFIX+f'/groups/{gid}').json()
        assert next(p for p in detail['products'] if p['product_id']==ids['target'])['enabled'] is False
        assert client.post(PREFIX+f'/groups/{gid}/confirm',json={**args,'preview_hash':preview.json()['preview_hash'],
            'operation_key':'dept-pause','evidence':'虚构部门暂停','physical_match_confirmed':True}).json()['replayed'] is True
        _login(client,'preview-other-customer')
        response=client.get(f"/api/warehouse/finished/products/{ids['target']}/candidates",params={'customer_id':ids['other_customer']})
        assert response.status_code==200,response.text
        assert response.json()['items']==[]
