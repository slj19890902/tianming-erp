import json
from pathlib import Path
from decimal import Decimal
import pytest
from app.services.pdf_ocr import OCRText
from app.services.pdf_sat_contract import parse_sat_contract


def sample(code='0008'):
    data=json.loads((Path(__file__).parent/'fixtures'/f'sat_contract_{code}.json').read_text(encoding='utf8'))
    return OCRText(data['text'],data['pages'])


@pytest.mark.parametrize('code,name,po,count,total',[
    ('0008','苏州驶安特汽车电子有限公司','SATE20260902007',8,'9971.00'),
    ('0032','苏州并作汽车电子有限公司','Unison202609034488',1,'592.00')])
def test_real_layout(code,name,po,count,total):
    result=parse_sat_contract(sample(code),code)
    assert result['customer_name']==name and result['customer_po']==po
    assert len(result['items'])==count
    assert sum(Decimal(i['amount']) for i in result['items'])==Decimal(total)
    assert result['integrity_check']['integrity_errors']==[]
    assert result['integrity_check']['integrity_status']=='unknown'
    assert all(i['unit']=='' and i['preserve_pdf_price'] for i in result['items'])
    if count==8:
        assert [i['quantity'] for i in result['items']]==['150','100','200','400','300','200','50','50']
        assert [i['delivery_date'] for i in result['items']]==['2026-09-26','2026-09-10','2026-09-26','2026-09-26','2026-09-26','2026-09-26','2026-09-10','2026-09-26']


def test_identity_conflict_and_unrelated():
    original=sample()
    changed=OCRText(str(original).replace('SATE20260902007','Unison202609034488'),original.pages)
    result=parse_sat_contract(changed,'x')
    assert result['customer_name'] is None
    assert result['customer_route']['status']=='needs_confirmation'
    assert parse_sat_contract('PO2026091401 天明','x') is None
    assert parse_sat_contract(original,'x',{'status':'locked','customer_name':'其他客户'})['customer_name'] is None
    assert parse_sat_contract(str(original),'x')['integrity_check']['integrity_status']=='failed'


@pytest.mark.parametrize('old,new',[('150','151'),('1,254.00','1,254.01'),('8.3600','9.3600')])
def test_amount_mismatch(old,new):
    original=sample()
    for block in original.pages[0]:
        if block['text']==old:
            block['text']=new
            break
    assert parse_sat_contract(original,'x')['integrity_check']['integrity_errors']


def test_missing_column_and_missing_date_fail_closed():
    original=sample()
    original.pages[0]=[b for b in original.pages[0] if b['text']!='数量']
    assert parse_sat_contract(original,'x')['integrity_check']['integrity_status']=='failed'
    original=sample()
    original.pages[0]=[b for b in original.pages[0] if b['text']!='2026/9/10']
    assert parse_sat_contract(original,'x')['integrity_check']['integrity_status']=='failed'


def test_existing_pipeline_uses_layout(monkeypatch):
    from app.services import pdf_parse_pipeline as pipeline, pdf_ocr
    monkeypatch.setattr(pipeline,'extract_text_from_pdf_bytes',lambda _: '')
    monkeypatch.setattr(pipeline,'ocr_pdf_bytes',lambda *a,**k:(sample(),'ocr_easyocr'))
    monkeypatch.setattr(pdf_ocr,'refine_contract_columns',lambda content,text:text)
    result=pipeline.parse_pdf_bytes(b'fixture','original.pdf',[])
    assert result.draft['item_count']==8 and result.ocr_attempted


def test_not_bound_to_sample_number_or_screen_geometry():
    original=sample()
    for page in original.pages:
        for block in page:
            block['box']=[[x*1.1+20,y*1.1+30] for x,y in block['box']]
    changed=OCRText(str(original).replace('SATE20260902007','SATE20261015009'),original.pages)
    result=parse_sat_contract(changed,'different.pdf')
    assert result['customer_po']=='SATE20261015009' and result['item_count']==8
    assert not result['integrity_check']['integrity_errors']


def test_customer_scoped_matching_no_guessed_prices_or_merging(tmp_path):
    from sqlalchemy.orm import Session
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.order_pdf_import import match_import_draft
    engine=create_sqlite_engine(tmp_path/'only-test.sqlite3')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer=Customer(name='苏州驶安特汽车电子有限公司',customer_code='SATE')
        other=Customer(name='苏州并作汽车电子有限公司',customer_code='UNI')
        db.add_all([customer,other]);db.flush()
        right=Product(customer_id=customer.id,product_code='SATJITP159049',customer_material_code='SATJITP159049',product_name='纸箱',sale_unit_price=Decimal('8'))
        wrong=Product(customer_id=other.id,product_code='SATJITP159049',customer_material_code='SATJITP159049',product_name='纸箱',sale_unit_price=Decimal('6.59'))
        ambiguous=Product(customer_id=customer.id,product_code='SATJITP600001',customer_material_code='SATJITP600001',product_name='TSB60外箱')
        db.add_all([right,wrong,ambiguous]);db.flush()
        draft=parse_sat_contract(sample(),'x')
        draft['items'][1]['unit_price']=None
        result=match_import_draft(db,draft)
        assert result['matched_customer_id']==customer.id and len(result['items'])==8
        assert result['items'][1]['matched_product_id']==right.id
        assert result['items'][1]['unit_price'] is None
        assert result['items'][6]['matched_product_id'] is None
        assert ambiguous.id in [p['id'] for p in result['items'][6]['product_candidates']]
        assert all(wrong.id not in [p['id'] for p in i['product_candidates']] for i in result['items'])
