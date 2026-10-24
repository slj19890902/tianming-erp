from datetime import date
from decimal import Decimal
import json
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from app.models.product import Product
from app.models.user import User
from app.models.stock_replenishment import StockReplenishmentOrder,StockReplenishmentOrderItem
from app.models.warehouse_inventory import InventoryLot,InventoryReservation,WarehouseLocation,InventoryMovement
from app.models.replenishment_production import ReplenishmentProductionJob
from app.services import replenishment_production as service
from app.services.warehouse_inventory import manual_semi_finished_in,WarehouseInventoryError
from tests.test_fixed_shelf import setup,rack_factory


@pytest.fixture
def case(setup):
    factory,pid,cid,locations=setup
    with factory() as db:
        p=db.get(Product,pid);p.flute_type=p.material.flute_type or 'B';p.layer_count=p.material.layer_count
        p.crease_type='毛片';p.default_material_code=p.material.code
        order=StockReplenishmentOrder(order_number='TEST-RSP',request_hash='hash',customer_id=cid,
            source_type='stock_warning',status='draft',supplier_name=p.material.supplier_name)
        db.add(order);db.flush()
        item=StockReplenishmentOrderItem(replenishment_order_id=order.id,target_inventory_type='semi_finished',
            procurement_route_snapshot='paperboard',reference_product_id=pid,customer_id=cid,material_id=p.material_id,
            product_code_snapshot=p.product_code,product_name_snapshot=p.product_name,
            material_code_snapshot=p.material.code,normalized_material_code=p.material.code,layer_count=p.layer_count,
            flute_type=p.flute_type,report_length_mm=1000,report_width_mm=300,crease_type='毛片',
            sheet_type='raw_board',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1,quantity=100)
        db.add(item);db.flush()
        from app.services.stock_purchase_identity import capture
        item.production_snapshot_json=json.dumps(capture(p,item))
        loc=db.get(WarehouseLocation,locations[0])
        lot=manual_semi_finished_in(db,location_id=loc.id,quantity=100,stock_date=date.today(),source_type='manual',
            material_id=p.material_id,material_code=p.material.code,layer_count=p.layer_count,flute_type=p.flute_type,
            board_length_mm=1000,board_width_mm=300,sheet_type='raw_board',supplier_name=p.material.supplier_name,
            customer_id=cid,customer_generic_eligible=True,crease_type='毛片',crease_left_mm=None,crease_middle_mm=None,
            crease_right_mm=None,cutting_note=None,remarks=None,operator_id=1,idempotency_key='rsp-fixture',
            expected_layout_version=loc.floor3_layout.version)
        db.commit()
        yield db,item,lot,db.get(User,1),db.get(WarehouseLocation,locations[1])


def plan(case,quantity=30,key='rsp-plan-001'):
    db,item,lot,actor,loc=case
    payload=dict(action='plan',quantity=quantity,operation_key=key,inventory_lot_id=lot.id,lot_version=lot.version,
        source_fingerprint=service.state(db,item)['source_fingerprint'])
    preview=service.preview(db,item.id,payload,actor)
    payload['preview_hash']=preview['preview_hash']
    value=service.mutate(db,item.id,payload,actor);db.commit()
    return value,payload


def complete(case,job,actual=20,key='rsp-complete-001'):
    db,item,lot,actor,loc=case
    payload=dict(action='complete',operation_key=key,job_id=job['id'],job_version=job['version'],
        lot_version=lot.version,actual_output=actual,location_id=loc.id,layout_version=loc.floor3_layout.version)
    value=service.mutate(db,item.id,payload,actor);db.commit()
    return value,payload


def test_plan_read_replay_cancel_quantity_and_demand_conservation(case):
    db,item,lot,actor,loc=case
    options=service.options(db,item.id,actor)
    assert options['remaining_output']==100 and len(options['candidates'])==1
    before=db.query(InventoryMovement).count()
    job,payload=plan(case)
    assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(70,30,0)
    assert service.state(db,item)['remaining_output']==70
    assert not service.state(db,item)['can_purchase']
    assert service.mutate(db,item.id,payload,actor)==job
    with pytest.raises(WarehouseInventoryError):service.mutate(db,item.id,{**payload,'quantity':29},actor)
    cancel=dict(action='cancel',operation_key='rsp-cancel-001',job_id=job['id'],job_version=job['version'],lot_version=lot.version)
    service.mutate(db,item.id,cancel,actor);db.commit()
    assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(100,0,0)
    assert service.state(db,item)['remaining_output']==100 and item.order.status=='confirmed'
    assert db.query(InventoryMovement).count()==before+2


def test_actual_short_output_recovers_gap_and_inherits_real_cost(case):
    db,item,lot,actor,loc=case
    job,_=plan(case)
    result,payload=complete(case,job,20)
    output=db.get(InventoryLot,result['output_lot_id'])
    assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(70,0,30)
    assert output.quantity_available==20 and output.source_ref_type=='replenishment_production'
    assert output.estimated_unit_cost_snapshot==lot.estimated_unit_cost_snapshot*Decimal(30)/Decimal(20)
    from app.services.inventory_valuation import frozen_cost
    assert frozen_cost(output,db)[0]==output.estimated_unit_cost_snapshot
    assert service.state(db,item)['remaining_sheet_quantity']==80
    assert service.state(db,item)['can_purchase']
    assert item.stocked_quantity==0 and item.order.status=='partially_stocked'
    from app.services.replenishment_receipt_progress import receipt_progress
    assert receipt_progress(db,item)['remaining_quantity']==80
    assert service.mutate(db,item.id,payload,actor)==result
    assert db.query(ReplenishmentProductionJob).count()==1


def test_full_output_completes_source_without_fake_receipt(case):
    db,item,lot,actor,loc=case
    job,_=plan(case,100)
    complete(case,job,100)
    assert item.order.status=='stocked' and item.stocked_quantity==0
    from app.models.incoming_receipt import IncomingReceiptItem
    assert db.query(IncomingReceiptItem).count()==0
    assert service.state(db,item)['remaining_output']==0


def test_stale_stock_and_atomic_audit_failure_leave_no_partial_writes(case,monkeypatch):
    db,item,lot,actor,loc=case
    job,_=plan(case)
    payload=dict(action='complete',operation_key='rsp-complete-failure',job_id=job['id'],job_version=job['version'],
        lot_version=lot.version,actual_output=20,location_id=loc.id,layout_version=loc.floor3_layout.version)
    count=db.query(InventoryLot).count()
    def fail(*a,**kw):raise RuntimeError('audit failed')
    monkeypatch.setattr('app.services.audit_log.append_audit_event',fail)
    with pytest.raises(RuntimeError):service.mutate(db,item.id,payload,actor)
    db.rollback()
    assert db.query(InventoryLot).count()==count
    assert (lot.quantity_reserved,lot.quantity_consumed)==(30,0)
    assert db.get(ReplenishmentProductionJob,job['id']).status=='pending'


def test_scrap_source_pending_cancel_and_completed_coverage(case,monkeypatch):
    from app.services import scrap_production as scrap
    db,item,lot,actor,loc=case
    monkeypatch.setattr('app.services.processing_cost.estimate_standard_processing_cost',
        lambda *a,**kw:dict(calculation_status='calculated',estimated_processing_cost='12',missing_items=[]))
    payload=dict(action='plan',product_id=item.reference_product_id,stock_replenishment_item_id=item.id,
        quantity=40,location_id=loc.id,stock_date='2026-10-24',variant_id=None,remarks='',operation_key='rsp-scrap-plan',preview_hash='')
    preview=scrap.preview(db,payload,actor)
    payload['preview_hash']=preview['preview_hash']
    result=scrap.mutate(db,payload,actor);db.commit()
    assert service.state(db,item)['remaining_output']==60 and lot.quantity_available==100
    completed=scrap.mutate(db,{**payload,'action':'complete','operation_key':'rsp-scrap-complete',
        'job_id':result['id'],'job_version':result['version']},actor);db.commit()
    assert service.state(db,item)['remaining_output']==60 and service.state(db,item)['can_purchase']
    output=db.get(InventoryLot,completed['output_lot_id'])
    assert output.quantity_available==40 and output.estimated_unit_cost_snapshot==Decimal('.3')
    assert item.stocked_quantity==0


def test_partial_source_purchase_freezes_actual_gap_and_receipt_plan(case):
    from types import SimpleNamespace
    from app.models.supplier import Supplier
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.procurement_source import ProcurementSourceLink
    from app.services.supplier_master import normalize_supplier_identity
    from app.services.unified_procurement import pending_stock_rows,attach_stock_sources
    db,item,lot,actor,loc=case
    name=item.order.supplier_name
    db.add(Supplier(standard_name=name,normalized_name=normalize_supplier_identity(name)))
    purchase=SupplierRequisitionOrder(order_number='RSP-PO',supplier_name=name,total_quantity=0,requisition_qty=0,status='confirmed')
    db.add(purchase);db.commit()
    job,_=plan(case,30)
    row=next(r for r in pending_stock_rows(db,actor) if r['stock_replenishment_item_id']==item.id)
    assert row['requisition_qty']==70 and not row['can_purchase']
    selection=SimpleNamespace(stock_replenishment_item_id=item.id,source_fingerprint=row['source_fingerprint'])
    with pytest.raises(HTTPException,match='实际完工'):attach_stock_sources(db,purchase=purchase,selections=[selection],user=actor)
    complete(case,job,20)
    row=next(r for r in pending_stock_rows(db,actor) if r['stock_replenishment_item_id']==item.id)
    assert row['requisition_qty']==80 and row['can_purchase']
    selection.source_fingerprint=row['source_fingerprint']
    attach_stock_sources(db,purchase=purchase,selections=[selection],user=actor);db.commit()
    link=db.scalar(select(ProcurementSourceLink))
    assert link.source_quantity==80 and purchase.requisition_qty==80 and item.quantity==100
    from app.services.incoming_receipts import stock_source_summary
    assert stock_source_summary(db,f'sr{item.id}')['planned_quantity']==80
    assert service.options(db,item.id,actor)['blocked_reason']
    with pytest.raises(WarehouseInventoryError,match='采购'):
        service.preview(db,item.id,dict(quantity=1,inventory_lot_id=lot.id,lot_version=lot.version,
            source_fingerprint=service.state(db,item)['source_fingerprint']),actor)


def test_void_requires_all_pending_sources_cancelled(case):
    from app.api.requisition import void_stock_replenishment_order
    db,item,lot,actor,loc=case
    job,_=plan(case)
    with pytest.raises(HTTPException,match='生产安排'):void_stock_replenishment_order(item.order.id,db,actor)
    service.mutate(db,item.id,dict(action='cancel',operation_key='rsp-void-cancel',job_id=job['id'],
        job_version=job['version'],lot_version=lot.version),actor);db.commit()
    assert void_stock_replenishment_order(item.order.id,db,actor)['status']=='voided'


def test_source_scope_is_checked_before_result_replay(case,monkeypatch):
    db,item,lot,actor,loc=case
    _,payload=plan(case)
    def deny(*a,**kw):raise HTTPException(403,'customer denied')
    monkeypatch.setattr('app.api.requisition._require_stock_replenishment_order_access',deny)
    with pytest.raises(HTTPException,match='customer denied'):service.checked_command(db,item.id,payload,actor)


def test_frozen_dimensions_survive_changed_master_and_bad_flute_is_excluded(case):
    db,item,lot,actor,loc=case
    p=db.get(Product,item.reference_product_id);p.report_length_mm=5000;p.report_width_mm=5000;db.commit()
    assert service.options(db,item.id,actor)['candidates'][0]['inventory_lot_id']==lot.id
    lot.semi_finished_detail.flute_type='E';db.commit()
    assert service.options(db,item.id,actor)['candidates']==[]


def test_migration_empty_roundtrip_and_new_fact_downgrade_refusal(tmp_path):
    import importlib.util
    from sqlalchemy import create_engine
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    from pathlib import Path
    path=Path(__file__).parents[1]/'alembic/versions/fb1024rs_replenishment_production_sources.py'
    spec=importlib.util.spec_from_file_location('rsp_migration',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    engine=create_engine('sqlite:///'+str(tmp_path/'migration.sqlite'))
    with engine.connect() as conn:
        for table in ('stock_replenishment_order_items','inventory_reservations','products','inventory_lots','users'):
            conn.exec_driver_sql(f'CREATE TABLE {table}(id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE scrap_production_jobs(id INTEGER PRIMARY KEY,order_item_id INTEGER, snapshot_json TEXT NOT NULL)')
        conn.exec_driver_sql('CREATE INDEX old_scrap_index ON scrap_production_jobs(order_item_id)')
        conn.exec_driver_sql("CREATE TRIGGER old_scrap_no_delete BEFORE DELETE ON scrap_production_jobs BEGIN SELECT RAISE(ABORT,'retain'); END")
        conn.exec_driver_sql("INSERT INTO scrap_production_jobs VALUES (1,NULL,'original')")
        conn.commit()
        before=conn.exec_driver_sql("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").all()
        module.op=Operations(MigrationContext.configure(conn))
        module.upgrade();conn.commit();module.downgrade();conn.commit()
        assert conn.exec_driver_sql("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").all()==before
        assert conn.exec_driver_sql('SELECT * FROM scrap_production_jobs').all()==[(1,None,'original')]
        module.upgrade();conn.commit()
        conn.exec_driver_sql("INSERT INTO stock_replenishment_production_jobs VALUES(1,1,1,1,'{}',1,1,0,NULL,'cancelled',1,1,CURRENT_TIMESTAMP)")
        conn.commit()
        with pytest.raises(RuntimeError,match='destructive downgrade'):module.downgrade()
        assert conn.exec_driver_sql('SELECT count(*) FROM stock_replenishment_production_jobs').scalar()==1


def test_reviewable_die_cut_lot_is_available_for_this_source_without_master_rewrite(case):
    from app.models.warehouse_goods import WarehouseGoodsProfile
    db,item,lot,actor,loc=case
    p=db.get(Product,item.reference_product_id)
    p.box_style='模切内盒'
    from app.services.stock_purchase_identity import capture
    item.production_snapshot_json=json.dumps(capture(p,item))
    profile=dict(scope='customers',customer_ids=[item.customer_id],product_ids=[],processing='die_cut',
        material_confidence='confirmed',verified_material_id=None,material_code=item.material_code_snapshot,
        face_paper='kraft',mold_tool_id=None)
    db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(profile)));db.commit()
    result=service.options(db,item.id,actor)
    assert result['candidates'] and result['candidates'][0]['qualification']['processed_match']['usage_confirmation_required']
    job,_=plan(case,30)
    assert lot.quantity_reserved==30
    complete(case,job,30)
    assert json.loads(db.get(WarehouseGoodsProfile,lot.id).data_json)['product_ids']==[]


def test_two_piece_and_multi_yield_use_physical_output_once(case):
    db,item,lot,actor,loc=case
    item.pieces_per_box=2;item.stock_yield_per_sheet=2
    p=db.get(Product,item.reference_product_id);p.pieces_per_box=2
    from app.services.stock_purchase_identity import capture
    item.production_snapshot_json=json.dumps(capture(p,item))
    lot.semi_finished_detail.pieces_per_box=2;lot.semi_finished_detail.stock_yield_per_sheet=2
    db.commit()
    job,_=plan(case,30)
    assert job['expected_output']==30
    complete(case,job,20)
    value=service.state(db,item)
    assert value['required_output']==100 and value['remaining_output']==80 and value['remaining_sheet_quantity']==80


def test_api_contract_result_replay_and_permission_boundary(case):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps import get_db,get_current_user
    from app.api.replenishment_production import router
    db,item,lot,actor,loc=case
    app=FastAPI();app.include_router(router,prefix='/api/production')
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[get_current_user]=lambda:actor
    base=f'/api/production/stock-replenishment/{item.id}/production-sources'
    with TestClient(app) as client:
        view=client.get(base);assert view.status_code==200,view.text
        body=dict(action='plan',operation_key='rsp-http-plan',source_fingerprint=view.json()['source_fingerprint'],
            inventory_lot_id=lot.id,lot_version=lot.version,quantity=15)
        preview=client.post(base+'/preview',json=body);assert preview.status_code==200,preview.text
        body['preview_hash']=preview.json()['preview_hash']
        created=client.post(base,json=body);assert created.status_code==200,created.text
        replay=client.post(base+'/result',json={'original_request':body})
        assert replay.json()['result']==created.json() and replay.headers['Cache-Control']=='no-store'
        assert len(client.get('/api/production/stock-replenishment/production-sources/jobs').json()['items'])==1
        actor.role='finance';db.commit()
        rejected=client.post(base,json={**body,'operation_key':'rsp-http-denied'})
        assert rejected.status_code==403,rejected.text
