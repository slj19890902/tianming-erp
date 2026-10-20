import json
from datetime import date, datetime
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.models.product import Product
from app.models.customer import Customer
from app.models.warehouse_inventory import WarehouseLocation, InventoryLot, FinishedGoodsInventoryDetail
from app.services.finished_stock_identity import product_basis
from app.services.pre_delivery_stock_preview import enrich_stock_preview
from tests.test_pre_delivery_current_match_r24 import facts


def stock(db, product, *, available=200, reserved=100, basis=None, owner=None, number='STOCK', status='active'):
    location=WarehouseLocation(location_code=number,location_name='南C货3',warehouse_type='finished')
    db.add(location);db.flush()
    lot=InventoryLot(lot_number=number,inventory_type='finished',warehouse_location_id=location.id,
        quantity_available=available,quantity_reserved=reserved,unit='boxes',status=status,
        source_type='stocktake',stock_date=date.today(),last_movement_at=datetime.now())
    db.add(lot);db.flush()
    db.add(FinishedGoodsInventoryDetail(inventory_lot_id=lot.id,owner_customer_id=owner or product.customer_id,
        is_general=False,product_id=product.id,inventory_code_snapshot=product.product_code,
        product_name_snapshot=product.product_name,physical_basis_json=basis or product_basis(product)))
    db.flush();return lot


def test_whole_stock_not_truncated_to_order_or_suggested_quantity_and_frozen_drawing(facts):
    db,batch,item,row=facts;product=db.get(Product,item.product_id)
    product.customer_drawing_number='NEW-DRAWING'
    item.customer_document_snapshot_json=json.dumps({'schema_version':1,'customer_drawing_number':'001-A'})
    lot=stock(db,product);db.commit()
    before=(lot.quantity_available,lot.quantity_reserved,item.quantity,item.delivered_quantity)
    row['final_delivery_qty']=450
    payload={'items':[row]};enrich_stock_preview(db,batch,payload)
    assert row['stock_on_hand_qty']==300  # Original order is only 20: not an order cap.
    assert row['customer_drawing_number']=='001-A'
    assert row['stock_locations'][0]['lot_id']==lot.id
    assert row['stock_locations'][0]['location_id']==lot.warehouse_location_id
    assert row['stock_locations'][0]['quantity']==300
    row['final_delivery_qty']=300;enrich_stock_preview(db,batch,payload)
    assert row['stock_on_hand_qty']==300
    assert before==(lot.quantity_available,lot.quantity_reserved,item.quantity,item.delivered_quantity)
    assert not db.new and not db.dirty and not db.deleted


def test_foreign_owner_and_mismatched_units_never_enter_same_product_stock(facts):
    db,batch,item,row=facts;product=db.get(Product,item.product_id)
    other=Customer(customer_number=2,customer_code='OTHER',name='别的客户');db.add(other);db.flush()
    stock(db,product,owner=other.id,number='FOREIGN')
    wrong=json.loads(product_basis(product));wrong['unit']='套'
    stock(db,product,basis=json.dumps(wrong),number='WRONG-UNIT')
    db.commit();enrich_stock_preview(db,batch,{'items':[row]})
    assert row['stock_on_hand_qty']==0 and row['stock_locations']==[]
    foreign=deepcopy(row);foreign['product_id']=product.id
    enrich_stock_preview(db,SimpleNamespace(customer_id=other.id),{'items':[foreign]})
    assert foreign['stock_on_hand_qty'] is None and foreign['customer_drawing_number']==''


def test_bom_parent_does_not_turn_legacy_single_stock_into_sets(composite_requisition_app):
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from tests.test_yl_bom_deduction_20261013 import legacy_parent_lot
    from app.models.order import OrderItem
    _,factory=composite_requisition_app;seed_graph(factory,quantity=300)
    lid=legacy_parent_lot(factory)
    with factory() as db:
        lot=db.get(InventoryLot,lid);correct=lot.finished_detail.physical_basis_json
        wrong=json.loads(correct);wrong['unit']='只';wrong['assembly']=[]
        lot.finished_detail.physical_basis_json=json.dumps(wrong);db.commit()
        row={'product_id':1,'order_item_id':1,'final_delivery_qty':450}
        enrich_stock_preview(db,SimpleNamespace(customer_id=1),{'items':[row]})
        assert row['stock_on_hand_qty']==0
        lot.finished_detail.physical_basis_json=correct;db.commit()
        enrich_stock_preview(db,SimpleNamespace(customer_id=1),{'items':[row]})
        assert row['stock_on_hand_qty']==810
        assert not db.new and not db.dirty

from tests.test_n039_composite_bom_requisition import composite_requisition_app


def test_frozen_external_quantity_basis_is_applied_to_physical_stock(facts):
    from app.services.delivery_quantities import order_basis, physical_stock_basis
    db,batch,item,row=facts;product=db.get(Product,item.product_id)
    item.supply_mode_snapshot='external_purchase';item.sales_unit_snapshot='箱'
    item.external_packaging_category_code_snapshot='test'
    item.external_packaging_specification_json_snapshot='{}'
    item.external_packaging_specification_summary_snapshot='隔离外购测试'
    item.external_packaging_candidate_snapshot_json='{}'
    item.external_packaging_product_version_snapshot=1
    item.external_packaging_purchase_unit_snapshot='片'
    item.external_packaging_order_quantity_basis_snapshot=1
    item.external_packaging_purchase_quantity_basis_snapshot=2
    item.external_packaging_quantity_per_finished_unit_snapshot=2
    basis=physical_stock_basis(product_basis(product),order_basis(item,batch.customer_id))
    stock(db,product,basis=basis);db.commit()
    enrich_stock_preview(db,batch,{'items':[row]})
    assert row['stock_on_hand_qty']==150 and row['stock_locations'][0]['quantity']==150
    assert not db.dirty


def test_upload_read_and_quantity_preview_return_live_stock_without_saving_edits(facts):
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportBatch, TianhuaPreDeliveryImportItem
    from app.api.tianhua_pre_delivery import _batch_response, readiness_preview, DraftRequest, DraftLine
    from app.models.user import User
    db,batch,item,row=facts;product=db.get(Product,item.product_id)
    product.customer_drawing_number='DRAWING-A';lot=stock(db,product)
    stored=TianhuaPreDeliveryImportBatch(batch_number='PRE-STOCK',filename='test.xlsx',customer_id=batch.customer_id,
        customer_name='测试客户',source_type='excel_upload',pre_delivery_date=date.today())
    db.add(stored);db.flush()
    line=TianhuaPreDeliveryImportItem(batch_id=stored.id,row_no=1,stock_code=product.product_code,
        image_qty=450,product_id=product.id,product_name=product.product_name,order_item_id=item.id,
        order_id=item.order_id,final_delivery_qty=450,status='stock_shortage')
    user=User(username='preview-admin',password_hash='test',real_name='隔离测试管理员',role='admin',is_active=True)
    db.add_all([line,user]);db.commit()
    initial=_batch_response(db,stored)['items'][0]
    assert initial['stock_on_hand_qty']==300 and initial['customer_drawing_number']=='DRAWING-A'
    output=readiness_preview(stored.id,DraftRequest(items=[DraftLine(item_id=line.id,row_no=1,final_delivery_qty=300)]),db,user)
    assert output['items'][0]['stock_on_hand_qty']==300
    assert output['items'][0]['stock_locations'][0]['lot_id']==lot.id
    assert line.final_delivery_qty==450 and not db.dirty and not db.new


def test_ordinary_reserved_stock_keeps_existing_identity_contract_after_caption_change(facts):
    db,batch,item,row=facts;product=db.get(Product,item.product_id)
    old=json.loads(product_basis(product));old['spec']='旧模切规格显示文字'
    stock(db,product,available=0,reserved=120,basis=json.dumps(old));db.commit()
    enrich_stock_preview(db,batch,{'items':[row]})
    assert row['stock_on_hand_qty']==120 and row['stock_locations'][0]['quantity']==120
