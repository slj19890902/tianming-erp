from datetime import date,datetime
from types import SimpleNamespace
import pytest
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot,FinishedGoodsInventoryDetail
from app.services.warehouse_inventory import finished_inventory_candidates_for_product
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def legacy_parent_lot(factory):
    with factory() as db:
        p=db.get(Product,1)
        lot=InventoryLot(lot_number='FICTION-LEGACY-PARENT',inventory_type='finished',warehouse_location_id=1,
            quantity_available=810,quantity_reserved=0,quantity_consumed=0,quantity_damaged=0,quantity_scrapped=0,
            unit='boxes',status='active',source_type='stocktake',stock_date=date(2026,10,8),last_movement_at=datetime(2026,10,8),version=1)
        db.add(lot);db.flush()
        from app.services.finished_stock_identity import product_basis
        db.add(FinishedGoodsInventoryDetail(inventory_lot_id=lot.id,owner_customer_id=1,is_general=False,
            product_id=1,inventory_code_snapshot=p.product_code,product_name_snapshot=p.product_name,physical_basis_json=product_basis(p)))
        db.commit();return lot.id


def test_legacy_composite_cannot_be_recommended_as_parent_stock(composite_requisition_app):
    _,factory=composite_requisition_app;lid=legacy_parent_lot(factory)
    with factory() as db:
        assert finished_inventory_candidates_for_product(db,customer_id=1,product_id=1)==[]
        lot=db.get(InventoryLot,lid)
        assert lot.quantity_available==810 and lot.quantity_reserved==0


def test_product_response_exposes_parent_stock_contract(composite_requisition_app):
    from app.api.products import _response,_summary_response
    _,factory=composite_requisition_app
    with factory() as db:
        product=db.get(Product,1);user=db.get(User,1)
        assert _response(product,user)['order_parent_inventory_eligible'] is False
        assert _summary_response(product,user)['order_parent_inventory_eligible'] is False
        assert _response(db.get(Product,2),user)['order_parent_inventory_eligible'] is True

@pytest.mark.parametrize('source,virtual,eligible',[(None,False,False),('assembled',False,True),('manufactured',False,True),('purchased',False,True),('separate',False,False),('assembled',True,False),('manufactured',True,False),('purchased',True,False)])
def test_all_parent_entry_points_use_same_role(composite_requisition_app,source,virtual,eligible):
    from app.models.multilevel_bom import ProductBomProfile
    from app.services.order_parent_stock import parent_inventory_eligible
    from app.api.orders import _physical_bom_parent
    _,factory=composite_requisition_app
    with factory() as db:
        p=db.get(Product,1);p.is_virtual_composite_parent=virtual
        if source:
            db.add(ProductBomProfile(product_id=1,source=source,material_mode='expand_children',delivery_mode='components' if source=='separate' else 'parent'));db.flush()
        profile=db.get(ProductBomProfile,1)
        assert parent_inventory_eligible(p,db=db)==eligible
        assert parent_inventory_eligible(p,profiles={1:profile})==eligible
        assert _physical_bom_parent(db,p)==eligible


def test_legacy_parent_api_does_not_create_a_rejected_plan(composite_requisition_app):
    from fastapi.testclient import TestClient
    from tests.test_n039_composite_bom_requisition import _login
    app,factory=composite_requisition_app;lid=legacy_parent_lot(factory)
    with TestClient(app) as client:
        _login(client)
        response=client.get('/api/warehouse/finished/products/1/candidates?customer_id=1')
        assert response.status_code==200,response.text
        assert response.json()['items']==[]
        wrong=client.post('/api/orders/inventory-draft-preview',json={'customer_id':1,'items':[{'client_line_id':'legacy-parent','product_id':1,'quantity':600,'reservation_plan':{'finished':[{'lot_id':lid,'quantity':600,'expected_version':1,'confirmed':True}],'semi':[]}}]})
        assert wrong.status_code==409,wrong.text
        assert '父项库存抵扣计划' in wrong.text
    with factory() as db:
        lot=db.get(InventoryLot,lid);assert lot.quantity_available==810 and lot.quantity_reserved==0

@pytest.mark.parametrize('sets,missing_units',[(270,0),(300,90)])
def test_finished_children_810_credit_real_demand_and_keep_assembly_separate(composite_requisition_app,_p181_published_map_identity,sets,missing_units):
    from decimal import Decimal
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.bom_auto_reservation import reserve_new_order_stock
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.api.requisition import _bom_pending_component_requirements
    _,factory=composite_requisition_app;mid,_=seed_graph(factory,quantity=sets)
    with factory() as db:
        material=db.get(Material,mid);material.quote_price=Decimal('2');material.price_unit='元/㎡';material.purchase_currency='CNY';material.purchase_tax_included=True
        lots=[]
        for pid,quantity in [(2,810),(3,1200)]:
            target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=pid)
            lots.append(manual_finished_in(db,customer_id=1,product_id=pid,location_id=target.location.id,quantity=quantity,
                stock_date=date(2026,10,8),source_type='stocktake',remarks='虚构分存成品',operator_id=1,
                idempotency_key=f'fictional-810-{pid}',expected_layout_version=target.layout_version))
        db.commit()
        ids=reserve_new_order_stock(db,order_item_id=1,operator_id=1);db.commit()
        assert len(ids)==2 and reserve_new_order_stock(db,order_item_id=1,operator_id=1)==[]
        requirements=read_graph_requirements(db,1)
        plan={p.product_id:p for p in requirements.plan.products}
        assert plan[2].credited_units==810 and plan[2].make_units==missing_units
        assert plan[3].make_units==0 and plan[1].make_units==sets
        pending=_bom_pending_component_requirements(db,db.get(OrderItem,1))
        assert sum(r['requisition_qty'] for r in pending)==missing_units
        assert [(l.quantity_available,l.quantity_reserved) for l in lots]==[(0,810),(1200-sets*4,sets*4)]


def test_physical_assembled_parent_still_saves_and_credits_children_once(composite_requisition_app,_p181_published_map_identity,monkeypatch):
    from decimal import Decimal
    from fastapi.testclient import TestClient
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from tests.test_n039_composite_bom_requisition import _login
    from app.models.material import Material
    from app.models.order import Order,OrderItem
    from app.models.warehouse_inventory import InventoryReservation,InventoryMovement
    from app.models.audit import OperationLog
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.core.time_contract import beijing_today
    from sqlalchemy import select,func
    app,factory=composite_requisition_app;mid,_=seed_graph(factory,quantity=600)
    from app.models.processing_cost import ProcessingCostSettings
    with factory() as db:
        settings=db.get(ProcessingCostSettings,1);settings.average_worker_monthly_salary=Decimal('5500');settings.average_worker_monthly_social_cost=Decimal('0');db.commit()
    with factory() as db:
        m=db.get(Material,mid);m.quote_price=Decimal('2');m.price_unit='元/㎡';m.purchase_currency='CNY';m.purchase_tax_included=True
        target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=1)
        lot=manual_finished_in(db,customer_id=1,product_id=1,location_id=target.location.id,quantity=810,
            stock_date=date(2026,10,8),source_type='stocktake',remarks='虚构已组装成品',operator_id=1,
            idempotency_key='fictional-assembled-810',expected_layout_version=target.layout_version)
        db.commit();lid,version=lot.id,lot.version
        models=(Order,OrderItem,InventoryReservation,InventoryMovement,OperationLog)
        counts={model:db.scalar(select(func.count()).select_from(model)) for model in models}
        assert [row.id for row in finished_inventory_candidates_for_product(db,customer_id=1,product_id=1)]==[lid]
    body={'customer_id':1,'order_date':beijing_today().isoformat(),'idempotency_key':'fictional-parent-save',
        'items':[{'client_line_id':'physical-parent','product_id':1,'quantity':600,'unit_price':'1','reservation_plan':{'finished':[{'lot_id':lid,'expected_version':version,'requested_qty':600,'confirmed':True}],'semi':[]}}]}
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client)
        with factory() as db:
            counts={model:db.scalar(select(func.count()).select_from(model)) for model in models}
        from app.api import orders
        with monkeypatch.context() as patch:
            def fail(*args,**kwargs):raise RuntimeError('隔离晚期成本故障')
            patch.setattr(orders,'freeze_order_item_material_cost',fail)
            failure=client.post('/api/orders',json=body)
            assert failure.status_code==500,failure.text
        with factory() as db:
            assert {model:db.scalar(select(func.count()).select_from(model)) for model in models}==counts
            lot=db.get(InventoryLot,lid);assert (lot.quantity_available,lot.quantity_reserved,lot.version)==(810,0,version)
        saved=client.post('/api/orders',json=body);assert saved.status_code==201,saved.text
        replay=client.post('/api/orders',json=body);assert replay.status_code in (200,201),replay.text
        assert replay.json()['id']==saved.json()['id']
        iid=saved.json()['items'][0]['id']
        changed={**body,'items':[{**body['items'][0],'quantity':601}]}
        assert client.post('/api/orders',json=changed).status_code==409
    with factory() as db:
        lot=db.get(InventoryLot,lid);assert (lot.quantity_available,lot.quantity_reserved)==(210,600)
        requirements=read_graph_requirements(db,iid)
        assert all(p.make_units==0 for p in requirements.plan.products)
        assert all(m.purchase_sheets==0 for m in requirements.plan.materials)
