from fastapi.testclient import TestClient
from app.models.customer import Customer
from tests.test_shared_finished_stock_api import shared_api
from tests.test_p1_18_pdf_inventory_contract import inventory_preview_app,_login
PREFIX='/api/warehouse/finished/shared-stock'


def test_missing_customer_reference_preview_confirm_and_non_admin(shared_api):
    app,factory,ids=shared_api
    with factory() as db:
        customer=Customer(name='虚构无主档海外部门',customer_code='MISSING-SALES');db.add(customer);db.commit();cid=customer.id
    args=dict(source_product_id=ids['shared_product'],product_ids=[ids['shared_product']],new_customer_ids=[cid],lot_ids=[ids['shared_lot']],auto_enroll=True)
    with TestClient(app) as client:
        _login(client,'preview-other-customer')
        assert client.post(PREFIX+'/customer-access/preview',json=args).status_code==403
        _login(client,'shared-admin')
        choices=client.get(PREFIX+f"/products/{ids['shared_product']}/customers").json()['items']
        assert next(c for c in choices if c['customer_id']==cid)['can_create_sales_reference'] is True
        response=client.post(PREFIX+'/customer-access/preview',json=args);assert response.status_code==200,response.text
        body={**args,'preview_hash':response.json()['preview_hash'],'operation_key':'api-reference',
            'evidence':'虚构样品相同','physical_match_confirmed':True}
        result=client.post(PREFIX+'/customer-access/confirm',json=body);assert result.status_code==200,result.text
        assert client.post(PREFIX+'/customer-access/confirm',json=body).json()['replayed']
        group=client.get(PREFIX+'/groups/'+str(result.json()['group_id'])).json()
        assert len(group['lots'])==1 and len(group['products'])==2
