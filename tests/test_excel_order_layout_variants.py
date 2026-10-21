from io import BytesIO
from decimal import Decimal
import pytest
from openpyxl import Workbook
from app.services.excel_document_import import parse_excel_document, ExcelImportError, SHORTAGE_LIST_TEMPLATE


def make(rows):
    book=Workbook(); sheet=book.active
    for row in rows: sheet.append(row)
    stream=BytesIO();book.save(stream);return stream.getvalue()


def parse(rows, **kwargs):
    return parse_excel_document(make(rows),'order.xlsx',document_type='order',expected_customer_code='GY',**kwargs)


def test_two_inserted_blank_columns_and_shipping_footer_preserve_exact_price():
    rows=[['箱','0632094','A2','80010631',30,None,None,'80010631','A',99,69,None,None,4.176,125.28],
          [None]*14+[125.28],[None,None,None,'光洋26.10.21',None,None,None,'出货10/26或27']]
    doc=parse(rows)
    assert len(doc.rows)==1 and doc.rows[0].stock_code=='80010631'
    assert doc.rows[0].unit_price==Decimal('4.176') and doc.rows[0].amount==Decimal('125.28')
    assert doc.rows[0].cells['unit_price'].coordinate=='N1'
    assert doc.delivery_date is None and '出货10/26或27' in ' '.join(doc.warnings)


def test_shortage_headers_reordered_missing_names_and_component_ratio_not_quantity():
    doc=parse([['欠品 数量','品目号','配套数','使用型号','规格/图号','品名'],
               [300,'C80010080','1/0.1','箱型','001',None], [50,'80010644',2]])
    assert doc.template==SHORTAGE_LIST_TEMPLATE
    assert [(r.stock_code,r.order_quantity) for r in doc.rows]==[('C80010080',300),('80010644',50)]
    assert doc.rows[0].cells['order_quantity'].coordinate=='A2' and doc.customer_po is None


@pytest.mark.parametrize('qty',[0,-1,1.5,'错误'])
def test_shortage_invalid_quantities_not_silently_dropped(qty):
    with pytest.raises(ExcelImportError): parse([['品目号','欠品数量'],['80010631',qty]])


def test_shortage_requires_customer_and_rejects_missing_code():
    with pytest.raises(ExcelImportError,match='先选择'):
        parse_excel_document(make([['品目号','欠品数量'],['80010631',2]]),'光洋.xls',document_type='order')
    with pytest.raises(ExcelImportError,match='存货编码'):
        parse([['品目号','欠品数量'],[None,2]])


def test_mixed_price_columns_fail_instead_of_underreading():
    with pytest.raises(ExcelImportError,match='单价列不统一'):
        parse([['箱','1','A','80010631',2,None,None,'80010631','A',5,3,1.5,3],
               ['箱','1','A','80010631',2,None,None,'80010631','A',5,3,None,None,1.5,3]])
