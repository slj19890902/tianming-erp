from io import BytesIO
from decimal import Decimal
import fitz
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select
from tests.test_p1_130_statement_invoice_finance import p1_130_app, _login

PO='UAT-R14-000000000000000012-20261007'
CODE='00000000000000001201'

@pytest.mark.parametrize("count", [16, 70])
def test_pdf_long_identifiers_remain_inside_table_cells_and_keep_literal_text(count):
    from app.services.statement_pdf import render_customer_statement_pdf
    data=[dict(delivery_date='2026-10-07',delivery_number='UAT-A-20261007-002',customer_po=PO,product_code=CODE,product_name='<虚构纸箱&测试>',quantity=2,unit_price=Decimal('2.25'),amount=Decimal('4.50')) for _ in range(count)]
    content=render_customer_statement_pdf(title='对账单',subtitle='虚构客户 <甲&乙>',price_label='含税单价',amount_label='含税金额',rows=data,quantity_total=count*2,amount_total=Decimal("4.50")*count)
    doc=fitz.open(stream=content,filetype='pdf')
    assert len(doc)==1 if count==16 else len(doc)>1
    assert PO in doc[-1].get_text().replace("\n", ""), "Do not put a totals-only row on a separate page"
    text=''.join(p.get_text().replace('\n','') for p in doc)
    assert PO in text and CODE in text and '<虚构纸箱&测试>' in text and '虚构客户 <甲&乙>' in text
    for page in doc:
        assert '客户单号' in page.get_text(), 'repeat the actual table header on every page'
        vertical=[];horizontal=[]
        for d in page.get_drawings():
            for item in d['items']:
                if item[0]!='l':continue
                a,b=item[1:]
                if abs(a.x-b.x)<.01 and abs(a.y-b.y)>10:vertical.append(a.x)
                if abs(a.y-b.y)<.01 and abs(a.x-b.x)>100:horizontal.append(a.y)
        edges=sorted(set(round(x,2) for x in vertical)); ys=sorted(set(round(y,2) for y in horizontal))
        assert len(edges)==9 and len(ys)>2
        for block in page.get_text('dict')['blocks']:
            if 'lines' not in block:continue
            for line in block['lines']:
                for span in line['spans']:
                    x0,y0,x1,y1=span['bbox']
                    if y0<ys[1]-1 or y1>ys[-1]+1:continue
                    assert any(x0>=left-1 and x1<=right+1 for left,right in zip(edges,edges[1:])), f'Text crosses a cell boundary: {span["text"]}'

def test_customer_excel_keeps_long_codes_and_numeric_totals_with_wrapping(p1_130_app):
    from app.models.delivery import DeliveryItem
    app,factory=p1_130_app
    with factory() as db:
        for item in db.scalars(select(DeliveryItem)):
            item.customer_po_snapshot=PO;item.product_code_snapshot=CODE
        db.commit()
    with TestClient(app) as client:
        _login(client);response=client.get('/api/finance/statements/1/customer-export.xlsx')
        assert response.status_code==200
    sheet=load_workbook(BytesIO(response.content),data_only=False)['对账单']
    assert sheet['C5'].value==PO and sheet['D5'].value==CODE
    assert sheet['D5'].data_type=='s' and sheet['D5'].number_format=='@'
    assert sheet['C5'].alignment.wrap_text and sheet['D5'].alignment.wrap_text
    assert sheet['F7'].value==15 and sheet['H7'].value==200
