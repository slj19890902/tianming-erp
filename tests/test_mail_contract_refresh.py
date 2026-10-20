import copy
import json
from types import SimpleNamespace
import pytest
from app.services.pdf_ocr import OCRText
from app.services.pdf_sat_contract import parse_sat_contract
from app.services.email_pdf_queue import needs_recognition_refresh, RECOGNITION_VERSION


def contract(rows=2):
    labels=['序号','物料编码','品名','规格','数量','税前单价','税率','税后单价','价税合计','出货时间']
    def block(text,col,y,body=False):
        x=100+col*150
        item={'text':text,'confidence':.99,'box':[[x,y],[x+80,y],[x+80,y+20],[x,y+20]]}
        if body:item['contract_column']=col
        return item
    page=[block(label,i,100) for i,label in enumerate(labels)]
    for i in range(rows):
        # Same SKU, separate delivery dates: never merge the physical rows.
        values=[str(i+1),'3. SATJ1TP123456','测试包装','500*400*100mm',str(100*(i+1)),
                '4.690265','13%','5.3000',str(530*(i+1)),f'2026/11/{i+1}']
        page.extend(block(text,col,150+i*100,True) for col,text in enumerate(values))
    page.append(block('备注',0,200+rows*100))
    text=OCRText('采购订单 订单编号：SATE20261020001 乙方(需方)：苏州驶安特汽车电子有限公司 签订时间：2026/10/20',[page])
    text.contract_layout_verified=True
    return text


def test_ruled_contract_validates_rows_amounts_dates_without_document_total():
    result=parse_sat_contract(contract(),'arbitrary.pdf')
    assert result['integrity_check']['integrity_status']=='passed'
    assert result['recognition_status']=='recognized'
    assert [x['quantity'] for x in result['items']]==['100','200']
    assert [x['delivery_date'] for x in result['items']]==['2026-11-01','2026-11-02']
    assert result['customer_po']=='SATE20261020001'
    assert all(x['unit']=='' and x['preserve_pdf_price'] for x in result['items'])


@pytest.mark.parametrize('change',['missing_code','missing_sequence','wrong_amount','wrong_tax','wrong_date','repeat_sequence','unreadable_code'])
def test_bad_or_missing_physical_evidence_never_passes(change):
    source=contract();page=source.pages[0]
    target=next(b for b in page if b.get('contract_column')=={'missing_code':1,'missing_sequence':0,'wrong_amount':8,'wrong_tax':6,'wrong_date':9,'repeat_sequence':0,'unreadable_code':1}[change])
    if change.startswith('missing'):page.remove(target)
    else:target['text']={'wrong_amount':'531','wrong_tax':'3%','wrong_date':'2026/13/01','repeat_sequence':'2','unreadable_code':'3. SATJ1TP123456ę'}[change]
    assert parse_sat_contract(source,'file.pdf')['integrity_check']['integrity_status']=='failed'


def test_native_evidence_is_not_inferred_from_an_ordinary_text_draft():
    source=contract();source.contract_layout_verified=False
    assert parse_sat_contract(source,'SATE20261020001.pdf')['integrity_check']['integrity_status']!='passed'
    assert parse_sat_contract(OCRText('订单号缺失 乙方(需方)：苏州驶安特汽车电子有限公司',source.pages),'SATE20261020001.pdf') is None


def test_refresh_failed_cache_once_and_leave_existing_ready_cache():
    assert needs_recognition_refresh(None)
    assert needs_recognition_refresh(SimpleNamespace(status='improve',draft_json='{}'))
    assert needs_recognition_refresh(SimpleNamespace(status='improve',draft_json='broken'))
    assert not needs_recognition_refresh(SimpleNamespace(status='ready',draft_json='{}'))
    assert not needs_recognition_refresh(SimpleNamespace(status='improve',draft_json=json.dumps({'recognition_version':RECOGNITION_VERSION})))


def test_contract_family_prefix_only_is_normalized_with_verified_layout(tmp_path):
    from sqlalchemy.orm import Session
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.order_pdf_import import match_import_draft
    engine=create_sqlite_engine(tmp_path/'only-test.sqlite3');Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer=Customer(name='苏州驶安特汽车电子有限公司',customer_code='SATE')
        other=Customer(name='Other customer',customer_code='OTHER');db.add_all([customer,other]);db.flush()
        good=Product(customer_id=customer.id,product_code='SATJITP123456',customer_material_code='SATJITP123456',product_name='包装')
        wrong=Product(customer_id=other.id,product_code='SATJ1TP123456',customer_material_code='SATJ1TP123456',product_name='包装')
        db.add_all([good,wrong]);db.flush()
        draft=parse_sat_contract(contract(),'x')
        result=match_import_draft(db,draft)
        assert all(i['matched_product_id']==good.id for i in result['items'])
        assert result['items'][0]['unit_price']=='5.3000'
        assert result['items'][0]['production_notes']=='客户原单本行交期：2026-11-01'
        assert result['items'][1]['production_notes']=='客户原单本行交期：2026-11-02'
        assert match_import_draft(db,result)['items'][0]['production_notes']==result['items'][0]['production_notes']
        for i in draft['items']:i['product_code']='SATJ1TP12345G'
        assert all(i['matched_product_id'] is None for i in match_import_draft(db,draft)['items'])
        draft=parse_sat_contract(contract(),'x')
        db.add(Product(customer_id=customer.id,product_code='SATJ1TP123456',customer_material_code='SATJ1TP123456',product_name='另一包装'));db.flush()
        assert all(i['matched_product_id'] is None for i in match_import_draft(db,draft)['items'])
