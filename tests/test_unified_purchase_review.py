from fastapi.testclient import TestClient
from test_p1_73c_reported_item_void import reported_item_void_app, _login, _void


def test_review_filters_exact_document_and_retains_other_lines_after_void(reported_item_void_app):
    app, factory, ids = reported_item_void_app
    with TestClient(app) as client:
        _login(client)
        params={'document_id':ids['supplier_order_id'],'source_type':'supplier_order','page_size':20}
        response=client.get('/api/requisition/reported-items',params=params)
        assert response.status_code==200,response.text
        rows=response.json()['items']
        assert {r['item_id'] for r in rows}==set(ids['item_ids'])
        assert all(r['document_id']==ids['supplier_order_id'] for r in rows)
        assert client.get('/api/requisition/reported-items',params=dict(params,document_id=999999)).json()['items']==[]
        assert _void(client,ids['item_ids'][1],'review-single-void').status_code==200
        after=client.get('/api/requisition/reported-items',params=params).json()['items']
        assert len(after)==3
        assert sum(r['status']=='voided' for r in after)==1
