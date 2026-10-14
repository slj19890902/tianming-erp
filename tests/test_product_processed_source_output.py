"""Completed replenishment output is visible without inventing an allowed-product row."""
import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.product import Product
from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.stock_preparation import StockPreparationJob
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import (InventoryLot, InventoryLotTransfer, InventoryReservation,
    SemiFinishedInventoryDetail, SemiFinishedLotAllowedProduct, WarehouseLocation)
from tests.test_product_workbench import _app, _login, mobile_erp_app


def seed_output(db, ids, *, moved=False, invalid=None, verified_identity=False):
    p=db.get(Product,ids['product_two']);now=datetime(2026,10,10)
    place=db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code=='SF-TEMP'))
    order=StockReplenishmentOrder(order_number='CBW-PROCESSED',customer_id=p.customer_id,
        source_type='stock_warning',status='stocked')
    db.add(order);db.flush()
    item=StockReplenishmentOrderItem(replenishment_order_id=order.id,target_inventory_type='semi_finished',
        customer_id=p.customer_id,reference_product_id=p.id,product_code_snapshot=p.product_code,
        product_name_snapshot=p.product_name,quantity=30,stocked_quantity=30)
    receipt=IncomingReceipt(receipt_number='IR-PROCESSED',status='posted',received_at=now,idempotency_key='ir-processed')
    db.add_all([item,receipt]);db.flush()
    line=IncomingReceiptItem(receipt_id=receipt.id,stock_replenishment_item_id=item.id,
        planned_quantity=30,received_quantity=30,cumulative_received_quantity=30,variance_quantity=0,
        variance_type='matched',resolution_status='not_required',status='posted')
    db.add(line);db.flush()
    def sheet(number, available, ref_type, ref_id, owner=None):
        lot=InventoryLot(lot_number=number,inventory_type='semi_finished',warehouse_location_id=place.id,
            quantity_available=available,unit='sheets',status='active',source_type='transfer',
            source_ref_type=ref_type,source_ref_id=ref_id,stock_date=date(2026,10,10),last_movement_at=now)
        lot.semi_finished_detail=SemiFinishedInventoryDetail(owner_customer_id=owner or p.customer_id,
            material_code_snapshot='K=A',normalized_material_code='K=A',layer_count=3,flute_type='B',
            board_length_mm=800,board_width_mm=600,component_type='whole',sheet_type='net_sheet')
        db.add(lot);db.flush();return lot
    raw=sheet('PROCESSED-RAW',0,'stock_replenishment_receipt',line.id)
    line.received_inventory_lot_id=raw.id;item.inventory_lot_id=raw.id;raw.quantity_consumed=30
    reservation=InventoryReservation(reservation_number='PROCESSED-RES',inventory_lot_id=raw.id,
        reservation_type='semi_order',reserved_stock_quantity=30,consumed_stock_quantity=30,
        status='consumed',reserved_at=now,idempotency_key='processed-res')
    db.add(reservation);db.flush()
    job=StockPreparationJob(receipt_item_id=line.id,reservation_id=reservation.id,product_id=p.id,
        product_snapshot=json.dumps({'code':p.product_code,'product_id':p.id}),input_quantity=30,
        expected_output=30,actual_output=30,status='completed')
    db.add(job);db.flush()
    output=sheet('PROCESSED-OUTPUT',0 if moved else 30,'stock_preparation',job.id,
        db.get(Product,ids['other_product']).customer_id if invalid=='owner' else None)
    job.output_lot_id=output.id
    if invalid=='source':output.source_ref_id=job.id+1000
    if invalid=='status':job.status='cancelled'
    visible=output
    if moved:
        output.status='closed'
        visible=sheet('PROCESSED-MOVED',30,'stock_preparation',job.id)
        db.add(InventoryLotTransfer(source_lot_id=output.id,target_lot_id=visible.id,
            source_location_id=place.id,target_location_id=place.id,quantity=30,available_quantity=30,
            reserved_quantity=0,source_version_before=1,source_version_after=2,
            idempotency_key='processed-move',request_hash='f'*64,transferred_at=now))
    profile=dict(output_piece=True,product_ids=[p.id],customer_ids=[p.customer_id],
        scope='customers',usage_confirmed=True)
    if verified_identity:
        from app.services.finished_stock_identity import product_basis
        profile.update(processing='cut',remaining_processes=[],physical_basis=product_basis(p))
    db.add(WarehouseGoodsProfile(lot_id=visible.id,data_json=json.dumps(profile)))
    db.commit()
    assert not db.scalar(select(SemiFinishedLotAllowedProduct).where(SemiFinishedLotAllowedProduct.inventory_lot_id==visible.id))
    return visible.id,place.id,p.id


@pytest.mark.parametrize('moved',[False,True])
def test_completed_output_is_visible_in_search_and_location_without_double_count(mobile_erp_app,moved):
    app,ids,factory=_app(mobile_erp_app)
    with factory() as db:
        lot_id,location_id,product_id=seed_output(db,ids,moved=moved)
    with TestClient(app) as client:
        _login(client,'mobile-admin')
        found=client.get('/api/product-workbench/search',params={'q':'MB002'})
        assert found.status_code==200,found.text
        assert found.json()['items'][0]['inventory']['processed_component']['actual']==30
        detail=client.get(f'/api/product-workbench/products/{product_id}')
        assert detail.status_code==200,detail.text
        inventory=detail.json()['inventory']
        assert inventory['summary']['processed_component']['actual']==30
        assert inventory['summary']['finished']['actual']==0  # No implicit conversion to saleable cartons.
        assert inventory['summary']['semi_finished']['actual']==0  # Consumed material is not counted again.
        rows=[r for r in inventory['items'] if r['lot_id']==lot_id]
        assert len(rows)==1 and rows[0]['actual']==30 and rows[0]['unit']=='片'
        assert rows[0]['location_id']==location_id
        _login(client,'mobile-scoped')
        assert client.get(f'/api/product-workbench/products/{ids["other_product"]}').status_code==404
    with factory() as db:
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.version)==(30,0,1)
        assert not db.scalar(select(SemiFinishedLotAllowedProduct).where(SemiFinishedLotAllowedProduct.inventory_lot_id==lot_id))


@pytest.mark.parametrize('invalid',['owner','source','status'])
def test_output_requires_real_completed_same_customer_source(mobile_erp_app,invalid):
    from app.services.product_activity import source_lots
    app,ids,factory=_app(mobile_erp_app)
    with factory() as db:
        lot_id,_,product_id=seed_output(db,ids,invalid=invalid)
        assert lot_id not in {lot.id for lot in source_lots(db,db.get(Product,product_id))}


def test_processed_stock_is_ranked_before_empty_products_before_pagination(mobile_erp_app):
    app,ids,factory=_app(mobile_erp_app)
    with factory() as db:
        _,_,product_id=seed_output(db,ids,moved=True)
        stocked=db.get(Product,product_id)
        stocked.product_name='PROCESSED-RANK stocked'
        empty=db.get(Product,ids['other_product'])
        empty.product_name='PROCESSED-RANK empty'
        empty.length_mm=stocked.length_mm
        empty.width_mm=stocked.width_mm
        empty.height_mm=stocked.height_mm
        db.commit()
    with TestClient(app) as client:
        _login(client,'mobile-admin')
        response=client.get('/api/product-workbench/search',params={'q':'PROCESSED-RANK','page_size':1})
        assert response.status_code==200,response.text
        assert response.json()['total']==2
        assert response.json()['items'][0]['id']==product_id


@pytest.mark.parametrize('moved', [False, True])
def test_completed_output_covers_new_board_request_but_not_finished_stock(mobile_erp_app, moved):
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.services.stock_replenishment import stock_policy_dict
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        lot_id, _, product_id = seed_output(db, ids, moved=moved, verified_identity=True)
        product = db.get(Product, product_id)
        policy = InventoryStockPolicy(policy_name='processed output warning',
            target_inventory_type='finished', product_id=product_id,
            customer_id=product.customer_id, warning_quantity=5,
            target_quantity=20, active=True)
        db.add(policy); db.flush()
        summary = stock_policy_dict(db, policy)
        assert summary['available_quantity'] == 0
        assert summary['warning_triggered'] is True
        assert summary['processed_component_available_piece_quantity'] == 30
        assert summary['suggested_new_requisition_sheet_quantity'] == 0
        assert summary['replenishment_state'] == 'board_preparation_ready'
        db.get(InventoryLot, lot_id).quantity_available = 9
        db.flush()
        short = stock_policy_dict(db, policy)
        assert short['processed_component_available_piece_quantity'] == 9
        assert short['suggested_new_requisition_sheet_quantity'] > 0


def test_unverified_processed_output_cannot_suppress_new_board_request(mobile_erp_app):
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.services.stock_replenishment import stock_policy_dict
    _app_instance, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        _, _, product_id = seed_output(db, ids)
        product = db.get(Product, product_id)
        policy = InventoryStockPolicy(policy_name='unverified output warning',
            target_inventory_type='finished', product_id=product_id,
            customer_id=product.customer_id, warning_quantity=5,
            target_quantity=20, active=True)
        db.add(policy); db.flush()
        summary = stock_policy_dict(db, policy)
        assert summary['processed_component_available_piece_quantity'] == 0
        assert summary['suggested_new_requisition_sheet_quantity'] > 0


def test_changed_product_process_exposes_old_output_for_review_without_auto_offset(mobile_erp_app):
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.services.stock_replenishment import stock_policy_dict
    _app_instance, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        _, _, product_id = seed_output(db, ids, verified_identity=True)
        product = db.get(Product, product_id)
        policy = InventoryStockPolicy(policy_name='changed process warning',
            target_inventory_type='finished', product_id=product_id,
            customer_id=product.customer_id, warning_quantity=5,
            target_quantity=20, active=True)
        db.add(policy)
        product.production_process = '粘贴,模切'
        db.flush()
        summary = stock_policy_dict(db, policy)
        assert summary['processed_component_available_piece_quantity'] == 0
        assert summary['processed_component_identity_review_piece_quantity'] == 30
        assert summary['suggested_new_requisition_sheet_quantity'] > 0
        assert summary['replenishment_state'] == 'processed_identity_review'


@pytest.mark.parametrize('invalid', ['owner', 'source', 'status'])
def test_inconsistent_completed_output_cannot_suppress_other_demand(mobile_erp_app, invalid):
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.services.stock_replenishment import stock_policy_dict
    _app_instance, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        _, _, product_id = seed_output(db, ids, invalid=invalid, verified_identity=True)
        product = db.get(Product, product_id)
        policy = InventoryStockPolicy(policy_name='invalid output warning',
            target_inventory_type='finished', product_id=product_id,
            customer_id=product.customer_id, warning_quantity=5,
            target_quantity=20, active=True)
        db.add(policy); db.flush()
        summary = stock_policy_dict(db, policy)
        assert summary['processed_component_available_piece_quantity'] == 0
        assert summary['suggested_new_requisition_sheet_quantity'] > 0
