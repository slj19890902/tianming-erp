from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload, _parent_payload
from tests.test_manual_sheet_deduction_20261013 import seed, payload as manual_payload
from app.models.requisition import Requisition, RequisitionItem
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent


def seed_internal(sessions, *, sheet_contract=False):
    lot_id=seed(sessions,double=True,yield_factor=2 if sheet_contract else 1)
    with sessions() as db:
        snapshot=db.get(SalesOrderItemBomComponent,1)
        contract=None;mode='一开一';quantity=40
        if sheet_contract:
            from app.services.sheet_cutting_settings import SheetCuttingSettings
            settings=SheetCuttingSettings(1,1,2,True)
            snapshot.is_die_cut=True;snapshot.mold_max_yield_per_sheet=2
            snapshot.sheet_cutting_settings_snapshot={'schema_version':2,'whole':settings.to_dict()}
            contract=settings.contract(1000,700).to_snapshot();mode='一开二';quantity=20
        header=Requisition(requisition_number='BOM-INTERNAL-READ',requisition_date=date.today(),status='已报料')
        db.add(header);db.flush()
        row=RequisitionItem(requisition_id=header.id,order_item_id=1,requisition_qty=quantity,
            cardboard_len=1000,cardboard_width=700,special_process=mode,
            product_name_snapshot='内部组件',required_piece_qty=40,status='有效',sheet_cutting_snapshot=contract)
        db.add(row);db.flush()
        source=RequisitionItemBomSource(requisition_item_id=row.id,sales_order_item_bom_component_id=1,
            component_type='whole',order_set_quantity=snapshot.order_set_quantity,quantity_per_set=snapshot.quantity_per_set,
            required_piece_quantity=snapshot.required_piece_quantity,spare_sheet_quantity=0,
            calculated_purchase_quantity=quantity)
        db.add(source);db.flush();row_id=row.id;db.commit()
    return lot_id,row_id,contract


def preview_payload(contract=None):
    result={**_component_payload(1),'cardboard_len':1000,'cardboard_width':700}
    if contract:
        result.update(sheet_cutting_snapshot=contract,special_process='一开二')
    return result


@pytest.mark.parametrize('with_contract',[False,True])
def test_internal_unpurchased_bom_original_cutting_readonly_preview(composite_requisition_app,with_contract):
    app,sessions=composite_requisition_app;_,row_id,contract=seed_internal(sessions,sheet_contract=with_contract)
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/requisition/bom-sheet-cutting/preview',json=preview_payload(contract))
        assert response.status_code==200,response.text
        assert response.json()['remaining_required_piece_qty']==40
        assert response.json()['requisition_qty']==(20 if with_contract else 40)
        assert response.json()['carried_material_piece_qty']==0
    with sessions() as db:
        assert db.get(RequisitionItem,row_id).version==1
        assert db.get(RequisitionItem,row_id).requisition_qty==(20 if with_contract else 40)
        assert db.get(RequisitionItem,row_id).sheet_cutting_snapshot==contract


def test_internal_bom_reserve_then_refresh_keeps_remaining_and_new_fingerprint(composite_requisition_app):
    app,sessions=composite_requisition_app;_,row_id,_=seed_internal(sessions)
    base='/api/warehouse/semi-finished/bom-components/1'
    with TestClient(app) as client:
        _login(client)
        first=client.post('/api/requisition/bom-sheet-cutting/preview',json=preview_payload())
        assert first.status_code==200,first.text
        viewed=client.get(base+'/manual-inventory');assert viewed.status_code==200,viewed.text
        adopted=client.post(base+'/manual-reserve',json=manual_payload(viewed.json(),7,'bom-internal-read-seven'))
        assert adopted.status_code==200,adopted.text
        refreshed=client.post('/api/requisition/bom-sheet-cutting/preview',json=preview_payload())
        assert refreshed.status_code==200,refreshed.text
        assert refreshed.json()['remaining_required_piece_qty']==33
        assert refreshed.json()['requisition_qty']==33
        assert refreshed.json()['carried_material_piece_qty']==0
        assert refreshed.json()['purpose_plan_fingerprint']!=first.json()['purpose_plan_fingerprint']
        repeated=client.post('/api/requisition/bom-sheet-cutting/preview',json=preview_payload())
        assert repeated.json()['purpose_plan_fingerprint']==refreshed.json()['purpose_plan_fingerprint']
        duplicate=client.post('/api/requisition/batches',json={'supplier_name':'N039 供应商','items':[
            _parent_payload(),{**preview_payload(),'requisition_qty':33},_component_payload(2)]})
        assert duplicate.status_code==409,duplicate.text
    with sessions() as db:
        assert db.get(RequisitionItem,row_id).requisition_qty==33
        assert db.get(RequisitionItem,row_id).version==2
        assert db.scalar(select(func.count(Requisition.id)))==1


@pytest.mark.parametrize('change',[{'special_process':'一开二'},{'cardboard_len':1001},{'cardboard_width':701}])
def test_internal_bom_cannot_change_original_cutting_or_supplier_size(composite_requisition_app,change):
    app,sessions=composite_requisition_app;_,row_id,_=seed_internal(sessions)
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/requisition/bom-sheet-cutting/preview',json={**preview_payload(),**change})
        assert response.status_code==409,response.text
    with sessions() as db:assert db.get(RequisitionItem,row_id).version==1


@pytest.mark.parametrize('blocker',['frozen','supplier','receipt'])
def test_frozen_purchased_or_received_bom_still_rejects_preview(composite_requisition_app,blocker):
    app,sessions=composite_requisition_app;_,row_id,_=seed_internal(sessions)
    with sessions() as db:
        row=db.get(RequisitionItem,row_id)
        if blocker=='frozen':row.purpose_contract_status='frozen'
        if blocker=='supplier':
            from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
            from app.models.procurement_source import ProcurementSourceLink
            header=SupplierRequisitionOrder(order_number='BOM-PURCHASED',status='confirmed',total_quantity=40,requisition_qty=40)
            db.add(header);db.flush()
            line=SupplierRequisitionOrderItem(supplier_order_id=header.id,quantity=40,requisition_qty=40,cutting_mode='一开一')
            db.add(line);db.flush()
            db.add(ProcurementSourceLink(supplier_item_id=line.id,material_requisition_item_id=row.id,
                source_quantity=40,source_snapshot_json='{}',created_by=1,status='active'))
        if blocker=='receipt':
            from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
            header=IncomingReceipt(receipt_number='BOM-RECEIVED',received_at=datetime.now(),idempotency_key='bom-received-preview')
            db.add(header);db.flush()
            db.add(IncomingReceiptItem(receipt_id=header.id,order_id=1,order_item_id=1,requisition_id=row.requisition_id,
                requisition_item_id=row.id,planned_quantity=40,received_quantity=40,cumulative_received_quantity=40,
                variance_quantity=0,variance_type='matched',resolution_status='not_required',status='posted'))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/requisition/bom-sheet-cutting/preview',json=preview_payload())
        assert response.status_code==409,response.text
    with sessions() as db:
        assert db.get(RequisitionItem,row_id).version==1
        assert db.get(RequisitionItem,row_id).requisition_qty==40


@pytest.mark.parametrize('kind',['splits','molds','theoretical','trim'])
def test_internal_explicit_contract_changes_return_409_without_writes(composite_requisition_app,kind):
    from app.services.sheet_cutting_settings import SheetCuttingSettings
    app,sessions=composite_requisition_app;_,row_id,contract=seed_internal(sessions,sheet_contract=True)
    request=preview_payload(contract)
    if kind=='splits':
        request['sheet_cutting_snapshot']=SheetCuttingSettings(2,1,2,True).contract(1000,700).to_snapshot()
    elif kind=='molds':request['actual_yield_per_sheet']=1
    elif kind=='theoretical':
        request['sheet_cutting_snapshot']=SheetCuttingSettings(1,1,2,True).contract(1001,700).to_snapshot()
    elif kind=='trim':
        request['sheet_cutting_snapshot']=SheetCuttingSettings(1,1,2,True,1000,710).contract(1000,700).to_snapshot()
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/requisition/bom-sheet-cutting/preview',json=request)
        assert response.status_code==409,response.text
    with sessions() as db:
        row=db.get(RequisitionItem,row_id)
        assert row.version==1 and row.requisition_qty==20 and row.sheet_cutting_snapshot==contract


def test_internal_supplier_trim_contract_reads_original_facts(composite_requisition_app):
    from app.services.sheet_cutting_settings import SheetCuttingSettings
    app,sessions=composite_requisition_app;_,row_id,_=seed_internal(sessions,sheet_contract=True)
    settings=SheetCuttingSettings(1,1,2,True,1000,720)
    contract=settings.contract(1000,700).to_snapshot()
    with sessions() as db:
        row=db.get(RequisitionItem,row_id);row.cardboard_width=720;row.sheet_cutting_snapshot=contract
        snapshot=db.get(SalesOrderItemBomComponent,1)
        snapshot.snapshot_component_crease_type='毛片'
        snapshot.sheet_cutting_settings_snapshot={'schema_version':3,'whole':settings.to_dict()}
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/requisition/bom-sheet-cutting/preview',json={**preview_payload(contract),'cardboard_width':720})
        assert response.status_code==200,response.text
        assert Decimal(str(response.json()['report_width_mm']))==Decimal(720)
        assert response.json()['sheet_cutting_snapshot']==contract
    with sessions() as db:
        assert db.get(RequisitionItem,row_id).version==1
        assert db.get(RequisitionItem,row_id).sheet_cutting_snapshot==contract


def test_schema5_internal_bom_inventory_refresh_does_not_credit_internal_as_purchase(composite_requisition_app):
    from sqlalchemy import delete
    from app.models.product import Product
    from app.models.user import User
    from app.models.material import Material
    from app.services.multilevel_bom_orders import freeze_master_order_bom
    from tests.test_multilevel_bom_master import save
    from app.api.requisition import _bom_snapshot_requirements
    app,sessions=composite_requisition_app;seed(sessions,double=True)
    with sessions() as db:
        db.execute(delete(SalesOrderItemBomComponent))
        actor=db.get(User,1)
        material=Material(code='K616K',supplier_name='N039 供应商',layer_count=5)
        db.add(material);db.flush()
        for product_id in (1,2,3):
            product=db.get(Product,product_id)
            product.report_length_mm=1000;product.report_width_mm=700;product.material_id=material.id
            product.layer_count=5;product.flute_type='AB'
        save(db,actor,1,'assembled',[(2,3,'assembly'),(3,4,'assembly')])
        compiled=freeze_master_order_bom(db,order_item_id=1,actor=actor)
        db.flush()
        snapshot=next(row for row in compiled.snapshots if row.component_product_id==2)
        assert snapshot.snapshot_schema_version==5
        demand=_bom_snapshot_requirements(db,snapshot)
        header=Requisition(requisition_number='BOM-INTERNAL-SCHEMA5',requisition_date=date.today(),status='已报料')
        db.add(header);db.flush()
        row=RequisitionItem(requisition_id=header.id,order_item_id=1,requisition_qty=demand['requisition_qty'],
            cardboard_len=demand['report_length_mm'],cardboard_width=demand['report_width_mm'],
            special_process=demand['cutting_mode'],product_name_snapshot='多级内部子件',
            required_piece_qty=demand['required_piece_quantity'],status='有效')
        db.add(row);db.flush();row_id=row.id
        db.add(RequisitionItemBomSource(requisition_item_id=row.id,sales_order_item_bom_component_id=snapshot.id,
            component_type='whole',order_set_quantity=snapshot.order_set_quantity,quantity_per_set=snapshot.quantity_per_set,
            required_piece_quantity=demand['required_piece_quantity'],demand_basis='order_specific_pieces',spare_sheet_quantity=0,
            calculated_purchase_quantity=demand['requisition_qty']))
        db.commit();snapshot_id=snapshot.id;required=demand['required_piece_quantity']
    request={**_component_payload(snapshot_id),'cardboard_len':1000,'cardboard_width':700}
    base='/api/warehouse/semi-finished/bom-components/'+str(snapshot_id)
    with TestClient(app) as client:
        _login(client)
        first=client.post('/api/requisition/bom-sheet-cutting/preview',json=request)
        assert first.status_code==200,first.text
        assert first.json()['carried_material_piece_qty']==0
        assert first.json()['remaining_required_piece_qty']==required
        stock=client.get(base+'/manual-inventory');assert stock.status_code==200,stock.text
        adopt=client.post(base+'/manual-reserve',json=manual_payload(stock.json(),7,'schema5-internal-read-seven'))
        assert adopt.status_code==200,adopt.text
        refreshed=client.post('/api/requisition/bom-sheet-cutting/preview',json=request)
        assert refreshed.status_code==200,refreshed.text
        assert refreshed.json()['carried_material_piece_qty']==0
        assert refreshed.json()['remaining_required_piece_qty']==required-7
        assert refreshed.json()['purpose_plan_fingerprint']!=first.json()['purpose_plan_fingerprint']
    with sessions() as db:assert db.get(RequisitionItem,row_id).requisition_qty==required-7


def test_internal_preview_reports_conflicting_purchase_purpose_as_409(composite_requisition_app):
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder, SupplierRequisitionOrderItem, PurchasePurposeSourceSnapshot,
    )
    from app.models.procurement_source import ProcurementSourceLink
    app, sessions = composite_requisition_app
    _, row_id, _ = seed_internal(sessions)
    with sessions() as db:
        source = db.scalar(select(RequisitionItemBomSource).where(
            RequisitionItemBomSource.requisition_item_id == row_id))
        header = SupplierRequisitionOrder(order_number='BOM-PURPOSE-CONFLICT', status='confirmed',
            total_quantity=40, requisition_qty=40)
        db.add(header); db.flush()
        supplier = SupplierRequisitionOrderItem(supplier_order_id=header.id, quantity=40,
            requisition_qty=40, cutting_mode='一开一')
        db.add(supplier); db.flush()
        db.add(ProcurementSourceLink(supplier_item_id=supplier.id, material_requisition_item_id=row_id,
            source_quantity=40, source_snapshot_json='{}', created_by=1, status='active'))
        for label, formal_id, quantity in [
            ('supplier', {'supplier_requisition_order_item_id': supplier.id}, 40),
            ('material', {'material_requisition_item_id': row_id}, 1),
        ]:
            db.add(PurchasePurposeSourceSnapshot(snapshot_key='bom-conflict-'+label,
                allocation_group_key='bom-conflict-'+label, **formal_id, source_kind='bom_component',
                source_key='bom_component:1:whole', source_bom_requisition_source_id=source.id,
                customer_id=1, customer_name_snapshot='test', component_type='whole',
                source_finished_qty_snapshot=20, pieces_per_finished_snapshot=2,
                source_required_piece_qty_snapshot=40, source_semi_reserved_piece_qty_snapshot=0,
                source_effective_piece_qty_snapshot=quantity, yield_per_sheet_snapshot=1,
                group_effective_piece_qty_snapshot=quantity, group_authoritative_order_sheet_qty_snapshot=quantity,
                purchase_sheet_qty=quantity, order_purpose_sheet_qty=quantity, reserve_purpose_sheet_qty=0,
                calculation_rule_version='p1-80-v1', preview_fingerprint='a'*64,
                request_hash='b'*64, created_by=1))
        db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.post('/api/requisition/bom-sheet-cutting/preview', json=preview_payload())
        assert response.status_code == 409, response.text
        assert '用途合计' in response.text
    with sessions() as db:
        assert db.get(RequisitionItem, row_id).version == 1
        assert db.get(RequisitionItem, row_id).requisition_qty == 40
