"""HTTP regression on anonymous, disposable inventory only."""
from datetime import date
from decimal import Decimal
import copy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func,event
from app.models.material import Material
from app.models.order import OrderItem
from app.models.audit import OperationLog
from app.models.warehouse_inventory import InventoryLot,InventoryReservation,InventoryMovement,WarehouseLocation,OrderItemSemiRequirement
from app.models.user import User
from app.services.warehouse_inventory import manual_semi_finished_in
from app.services.multilevel_bom_requirements import read_graph_requirements
from app.services.bom_inventory_review import review,requisition_display
from tests.test_n039_composite_bom_requisition import composite_requisition_app,_login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph


@pytest.fixture
def paper_app(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app
    mid,_=seed_graph(factory,quantity=10)
    with factory() as db:
        material=db.get(Material,mid);material.quote_price=Decimal('2');material.price_unit='元/㎡';material.purchase_currency='CNY';material.purchase_tax_included=True
        location=db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code=='P181-RAW-STAGE'))
        lots=[]
        for index in (1,2):
            lot=manual_semi_finished_in(db,location_id=location.id,quantity=100,stock_date=date(2026,10,8),
                source_type='manual',material_code=material.code,layer_count=material.layer_count,flute_type=material.flute_type,
                board_length_mm=1000,board_width_mm=700,sheet_type='net_sheet',stock_yield_per_sheet=1,
                supplier_name=None,customer_id=1,crease_type=None,crease_left_mm=None,crease_middle_mm=None,crease_right_mm=None,
                cutting_note=None,remarks='虚构客户通用片料',operator_id=1,idempotency_key=f'fiction-paper-{index}',customer_generic_eligible=True,expected_layout_version=location.floor3_layout.version,allow_raw_material_staging=True)
            lots.append(lot.id)
        db.commit()
    return app,factory,lots


def request_for(client,key='fiction-adopt'):
    response=client.get('/api/warehouse/order-items/1/bom-inventory-review');assert response.status_code==200,response.text
    row=next(r for r in response.json()['rows'] if r['product_id']==2);paper=row['paper'][0];lot=paper['candidates'][0]
    return row['snapshot_id'],dict(component_type=paper['component_type'],review_hash=row['review_hash'],
        requested_requirement_quantity=paper['remaining_pieces'],lots=[dict(lot_id=lot['lot_id'],expected_version=lot['version'])],
        confirmed=True,override=False,warning_acknowledged_codes=lot['warning_codes'],idempotency_key=key)


def balances(factory):
    with factory() as db:
        return ([(l.id,l.quantity_available,l.quantity_reserved,l.quantity_consumed,l.version) for l in db.scalars(select(InventoryLot).order_by(InventoryLot.id))],
            [db.scalar(select(func.count()).select_from(model)) for model in (InventoryReservation,InventoryMovement,OrderItemSemiRequirement)],
            db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action_code=='BOM_PAPER_ADOPT')))


def test_atomic_adoption_exact_retry_conflicting_retry_and_no_fake_production(paper_app):
    app,factory,_=paper_app
    with TestClient(app) as client:
        _login(client);sid,payload=request_for(client)
        assert 'CUSTOMER_GENERIC_SEMI_FINISHED_STOCK' in payload['warning_acknowledged_codes']
        url=f'/api/warehouse/bom-components/{sid}/adopt-paper'
        response=client.post(url,json=payload);assert response.status_code==200,response.text
        result=response.json();assert (result['allocated_pieces'],result['allocated_sheets'])==(30,30)
        after=balances(factory)
        replay=client.post(url,json=payload);assert replay.status_code==200,replay.text
        assert replay.json()['replayed'] is True and balances(factory)==after
        changed=copy.deepcopy(payload);changed['lots'][0]['lot_id']+=1
        assert client.post(url,json=changed).status_code==409 and balances(factory)==after
    with factory() as db:
        req=read_graph_requirements(db,1)
        assert next(m for m in req.plan.materials if m.product_id==2).purchase_sheets==0
        assert next(p for p in req.plan.products if p.product_id==2).make_units==30
        assert next(p for p in req.plan.products if p.product_id==1).make_units==10
        assert db.get(OrderItem,1).requisition_status=='未报料'
        assert requisition_display(db,db.get(OrderItem,1),finished_reserved=0,has_bom=True)[0]=='未报料'


@pytest.mark.parametrize('change',[{'confirmed':False},{'warning_acknowledged_codes':[]},{'requested_requirement_quantity':31},{'review_hash':'0'*64},{'lots':[{'lot_id':999999,'expected_version':1}]}])
def test_refused_adoption_leaves_every_stock_and_requirement_unchanged(paper_app,change):
    app,factory,_=paper_app
    with TestClient(app) as client:
        _login(client);sid,payload=request_for(client);before=balances(factory)
        response=client.post(f'/api/warehouse/bom-components/{sid}/adopt-paper',json={**payload,**change})
        assert response.status_code in (403,404,409),response.text
        assert balances(factory)==before


def test_late_audit_failure_rolls_back_requirement_reservation_and_movement(paper_app,monkeypatch):
    app,factory,_=paper_app
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client);sid,payload=request_for(client);before=balances(factory)
        from app.services import audit_log
        with monkeypatch.context() as patch:
            def fail(*a,**k):raise RuntimeError('虚构审计写入故障')
            patch.setattr(audit_log,'append_audit_event',fail)
            failed=client.post(f'/api/warehouse/bom-components/{sid}/adopt-paper',json=payload)
            assert failed.status_code==500,failed.text
        assert balances(factory)==before
        saved=client.post(f'/api/warehouse/bom-components/{sid}/adopt-paper',json=payload)
        assert saved.status_code==200,saved.text


def test_preview_is_readonly_and_stage_and_version_changes_are_refused(paper_app):
    app,factory,lots=paper_app
    with factory() as db:
        statements=[]
        def record(c,cu,sql,*args):statements.append(sql.strip().split()[0].upper())
        event.listen(db.get_bind(),'before_cursor_execute',record)
        try:review(db,1)
        finally:event.remove(db.get_bind(),'before_cursor_execute',record)
        assert statements and set(statements)=={'SELECT'}
    with TestClient(app) as client:
        _login(client);sid,payload=request_for(client)
        with factory() as db:db.get(InventoryLot,payload['lots'][0]['lot_id']).version+=1;db.commit()
        before=balances(factory)
        assert client.post(f'/api/warehouse/bom-components/{sid}/adopt-paper',json=payload).status_code==409
        assert balances(factory)==before
        sid,payload=request_for(client)
        with factory() as db:db.get(OrderItem,1).requisition_status='已报料';db.commit()
        before=balances(factory)
        assert client.post(f'/api/warehouse/bom-components/{sid}/adopt-paper',json=payload).status_code==409
        assert balances(factory)==before


def test_all_paper_covered_is_no_procurement_but_still_requires_actual_assembly(paper_app):
    app,factory,_=paper_app
    with TestClient(app) as client:
        _login(client)
        for pid in (2,3):
            data=client.get('/api/warehouse/order-items/1/bom-inventory-review').json()
            row=next(r for r in data['rows'] if r['product_id']==pid);paper=row['paper'][0];lot=paper['candidates'][0]
            payload=dict(component_type=paper['component_type'],review_hash=row['review_hash'],requested_requirement_quantity=paper['remaining_pieces'],
                lots=[dict(lot_id=lot['lot_id'],expected_version=lot['version'])],confirmed=True,override=False,
                warning_acknowledged_codes=lot['warning_codes'],idempotency_key=f'fiction-all-{pid}')
            r=client.post(f"/api/warehouse/bom-components/{row['snapshot_id']}/adopt-paper",json=payload);assert r.status_code==200,r.text
    with factory() as db:
        label,hint=requisition_display(db,db.get(OrderItem,1),finished_reserved=0,has_bom=True)
        assert label=='无需报料' and '仍需' in hint
        assert db.get(OrderItem,1).requisition_status=='未报料'
        assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot)))==70
