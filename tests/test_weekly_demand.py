from io import BytesIO
from fastapi.testclient import TestClient
import pytest
from tests.test_p1_18_pdf_inventory_contract import inventory_preview_app, _login, _database_snapshot
from app.models.product import Product
from app.services.weekly_demand_import import parse_weekly_lines, source_lines


def test_two_panels_and_duplicate_not_double_imported():
    rows, _ = parse_weekly_lines([('1', 'A5-2 80010340 30 80010340 A 270 240'),
                                  ('2', '80012052 5 80012052 B 0 -5'),
                                  ('3', '80010340 A 270 240'),
                                  ('4', 'A5-2 80010340 20')])
    assert [r['quantity'] for r in rows] == [30, 5, 20]
    assert rows[0]['source_stock_reference']['remaining'] == 240
    assert rows[1]['source_stock_reference']['remaining'] == -5
    assert rows[2]['warnings']


def test_excel_two_panels():
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(['位置','存货编码','需求数量',None,'存货编码','类别','现存','剩余'])
    wb.active.append(['A5-2','80010340',30,None,'80010340','A',270,240])
    buffer=BytesIO(); wb.save(buffer)
    rows,_=parse_weekly_lines(source_lines(buffer.getvalue(), '.xlsx'))
    assert len(rows)==1 and rows[0]['quantity']==30


def test_right_panel_only_rejected():
    with pytest.raises(ValueError, match='没有识别'):
        parse_weekly_lines([('1','80010340 A 270 240')])


def test_stock_cumulative_read_only_and_location(inventory_preview_app):
    app,factory,ids=inventory_preview_app
    before=_database_snapshot(factory)
    with TestClient(app) as client:
        _login(client,'preview-allowed')
        response=client.post('/api/orders/demand-stock-preview',json=dict(customer_id=ids['customer'],items=[
            dict(client_line_id='a',product_id=ids['partial_product'],quantity=40),
            dict(client_line_id='b',product_id=ids['partial_product'],quantity=30),
            dict(client_line_id='c',product_id=ids['partial_product'],quantity=5)]))
        assert response.status_code==200,response.text
        rows=response.json()['items']
        assert [r['projected_remaining'] for r in rows]==[20,-10,-15]
        assert [r['batch_remaining'] for r in rows]==[-15,-15,-15]
        assert rows[0]['locations'][0]['location_name']=='P1-18成品测试库位'
    assert _database_snapshot(factory)==before


@pytest.mark.parametrize('username',['preview-no-warehouse','preview-other-customer'])
def test_stock_permissions(inventory_preview_app,username):
    app,_,ids=inventory_preview_app
    with TestClient(app) as client:
        _login(client,username)
        response=client.post('/api/orders/demand-stock-preview',json=dict(customer_id=ids['customer'],items=[dict(client_line_id='a',product_id=ids['full_product'],quantity=1)]))
        assert response.status_code==403


def test_weekly_preview_signed_and_scoped(inventory_preview_app):
    app,factory,ids=inventory_preview_app
    with factory() as db:
        product=db.get(Product,ids['partial_product']); product.product_code='80010340'; db.commit()
    before=_database_snapshot(factory)
    with TestClient(app) as client:
        _login(client,'preview-allowed')
        response=client.post(f"/api/orders/weekly-demand-preview?customer_id={ids['customer']}",data={'text':'A5-2 80010340 30 80010340 A 270 240'})
        assert response.status_code==200,response.text
        draft=response.json()
        assert len(draft['items'])==1
        assert draft['items'][0]['matched_product_id']==ids['partial_product']
        assert draft['preview_safety_token'] and draft['file_hash']
        assert draft['delivery_date'] is None
        assert draft['items'][0]['source_stock_reference']['on_hand']==270
        assert client.post(f"/api/orders/weekly-demand-preview?customer_id={ids['other_customer']}",data={'text':'80010340 30'}).status_code==403
    assert _database_snapshot(factory)==before


def test_excel_signed_preview_and_duplicates(inventory_preview_app):
    import openpyxl
    app,factory,ids=inventory_preview_app
    with factory() as db:
        product=db.get(Product,ids['partial_product']); product.product_code='80010340'; db.commit()
    wb=openpyxl.Workbook(); wb.active.title='周需求'
    wb.active.append(['A5-2','80010340',30,None,'80010340','A',270,240])
    wb.active.append(['A5-2','80010340',20,None,'80010340','A',270,250])
    wb.active.append(['A5-2','8001034',10])
    buffer=BytesIO();wb.save(buffer)
    with TestClient(app) as client:
        _login(client,'preview-allowed')
        response=client.post(f"/api/orders/weekly-demand-preview?customer_id={ids['customer']}",files={'file':('week.xlsx',buffer.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        assert response.status_code==200,response.text
        data=response.json(); assert len(data['items'])==3
        assert data['items'][2]['matched_product_id'] is None
        assert data['items'][0]['source_sheet']=='周需求'
        assert [r['quantity'] for r in data['items']]==[30,20,10]


def test_stock_zero_invalid_duplicate_and_virtual(inventory_preview_app):
    app,factory,ids=inventory_preview_app
    with factory() as db:
        p=db.get(Product,ids['none_product']);p.is_virtual_composite_parent=True;db.commit()
    with TestClient(app) as client:
        _login(client,'preview-allowed')
        payload=dict(customer_id=ids['customer'],items=[dict(client_line_id='a',product_id=ids['partial_product'],quantity=0)])
        assert client.post('/api/orders/demand-stock-preview',json=payload).json()['items'][0]['batch_remaining']==60
        payload['items'][0]['quantity']=-1
        assert client.post('/api/orders/demand-stock-preview',json=payload).status_code==422
        payload['items'][0].update(quantity=5,product_id=ids['none_product'])
        assert client.post('/api/orders/demand-stock-preview',json=payload).json()['items'][0]['status']=='review'
        payload['items'].append(dict(payload['items'][0]))
        assert client.post('/api/orders/demand-stock-preview',json=payload).status_code==409
