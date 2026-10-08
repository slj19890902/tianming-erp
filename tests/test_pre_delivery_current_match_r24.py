from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import pytest
from sqlalchemy.orm import Session
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.services.tianhua_pre_delivery import RecognizedRow, preprocess_row
from app.services.pre_delivery_readiness import _refresh_bound_match_projection

@pytest.fixture
def facts(tmp_path):
    engine=create_sqlite_engine(tmp_path/'fictional-match.sqlite3')
    Base.metadata.create_all(engine)
    with Session(engine,expire_on_commit=False) as db:
        customer=Customer(customer_number=924,customer_code='YG',name='虚构R24回归客户',credit_limit=Decimal('0'))
        db.add(customer);db.flush()
        product=Product(customer_id=customer.id,product_code='UAT-R24-TEST',customer_material_code='UAT-R24-TEST',product_name='虚构R24纸箱',box_category='normal')
        db.add(product);db.flush()
        order=Order(order_number='R24-ORDER-1',customer_id=customer.id,customer_po='R24-PO',order_date=date(2026,10,7),delivery_date=date(2026,10,8),status='pending_delivery')
        db.add(order);db.flush()
        item=OrderItem(order_id=order.id,product_id=product.id,snapshot_product_name=product.product_name,quantity=20,delivered_quantity=0,unit_price=Decimal('3.6'),subtotal=Decimal('72'),material_status='pending',requisition_status='未报料')
        db.add(item);db.commit()
        batch=SimpleNamespace(customer_id=customer.id,pre_delivery_date=date(2026,10,8))
        recognition=RecognizedRow(1,'R24-PO UAT-R24-TEST 20','UAT-R24-TEST',20,'R24-PO')
        snapshot=preprocess_row(db,recognition,batch.pre_delivery_date,batch.customer_id)
        row={**snapshot,'order_no':snapshot['order_number'],'selected':False,'item_id':1,'source_row':11,'source_no':1}
        yield db,batch,item,row
    engine.dispose()

def test_stock_arrives_refreshes_display_without_adopting_or_persisting(facts):
    db,batch,item,row=facts
    assert row['status']=='stock_shortage' and row['available_qty']==0
    # Fixture transition for the pre-existing no-task received-material rule.
    # Real finished stock + reservation workflow is independently browser tested.
    item.material_status='received';db.commit()
    before=deepcopy(row);persisted=(item.quantity,item.delivered_quantity,item.material_status)
    _refresh_bound_match_projection(db,batch,row)
    assert (row['status'],row['status_label'],row['available_qty'],row['system_pending_qty'])==('ok','可送货',20,20)
    assert row['warning']==''
    assert all(row[k]==before[k] for k in ('order_item_id','product_id','order_id','selected','final_delivery_qty','source_row','source_no'))
    assert not db.dirty and persisted==(item.quantity,item.delivered_quantity,item.material_status)

def test_stock_loss_refreshes_back_to_shortage(facts):
    db,batch,item,row=facts
    item.material_status='received';db.commit();_refresh_bound_match_projection(db,batch,row)
    assert row['status']=='ok'
    item.material_status='pending';db.commit();_refresh_bound_match_projection(db,batch,row)
    assert row['status']=='stock_shortage' and row['available_qty']==0
    assert '当前可送数量 0' in row['warning'] and row['selected'] is False

def test_quantity_mismatch_is_retained_after_receiving(facts):
    db,batch,item,row=facts
    item.material_status='received';db.commit();row['image_qty']=19;row['raw_text']='R24-PO UAT-R24-TEST 19'
    _refresh_bound_match_projection(db,batch,row)
    assert row['status']=='qty_mismatch' and row['available_qty']==20
    assert '差异仅 1 个' in row['warning']

@pytest.mark.parametrize('terminal',['created','skipped','not_matched','ocr_failed'])
def test_refresh_does_not_revive_terminal_or_unmatched_rows(facts,terminal):
    db,batch,item,row=facts
    item.material_status='received';db.commit();row['status']=terminal;before=deepcopy(row)
    _refresh_bound_match_projection(db,batch,row)
    assert row==before and not db.dirty

def test_new_preferred_order_cannot_replace_saved_binding(facts):
    db,batch,item,row=facts
    original=row['order_item_id'];item.material_status='received'
    newer=Order(order_number='R24-ORDER-2',customer_id=batch.customer_id,customer_po='R24-PO',order_date=date(2026,10,7),delivery_date=date(2026,10,8),status='pending_delivery')
    db.add(newer);db.flush()
    other=OrderItem(order_id=newer.id,product_id=item.product_id,snapshot_product_name=item.snapshot_product_name,quantity=20,delivered_quantity=0,unit_price=Decimal('3.6'),subtotal=Decimal('72'),material_status='received',requisition_status='未报料')
    db.add(other);db.commit();before=deepcopy(row)
    _refresh_bound_match_projection(db,batch,row)
    assert row==before and row['order_item_id']==original and not db.dirty

def test_refresh_keeps_original_workbook_quantity_warning(facts):
    db,batch,item,row=facts
    issue='要求数 20 与发注数 19 不一致，请核对本次预送数量'
    row['source_payload']={'issues':[issue]}
    item.material_status='received';db.commit()
    _refresh_bound_match_projection(db,batch,row)
    assert row['status']=='ok' and row['warning']==issue
    assert row['source_payload']=={'issues':[issue]} and not db.dirty
