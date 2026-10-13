import json
from types import SimpleNamespace
import pytest
from app.models.product import Product
from app.models.order import OrderItem
from app.services.finished_stock_identity import product_basis, compiled_product_bases, matching_component_basis
from app.services.warehouse_display_units import lot_display_unit
from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph

@pytest.mark.parametrize('value', ['null', '[]', '1', '"text"', '{"quantity_basis":[]}','{"quantity_basis":"bad"}'])
def test_invalid_display_snapshot_does_not_crash_or_modify(value):
    lot=SimpleNamespace(source_ref_type='manual',unit='boxes',finished_detail=SimpleNamespace(product=None,physical_basis_json=value))
    assert lot_display_unit(lot)=='只'
    assert lot.finished_detail.physical_basis_json==value

@pytest.mark.parametrize('value', ['null', '[]', '1', '"text"'])
def test_invalid_receipt_cost_snapshot_readable(value):
    lot=SimpleNamespace(source_ref_type='direct_external_receipt',unit='boxes',cost_snapshot_detail_json=value,finished_detail=None)
    assert lot_display_unit(lot)=='只'
    assert lot.cost_snapshot_detail_json==value

def test_component_quantity_metadata_is_separate_from_identity(composite_requisition_app,_p181_published_map_identity):
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    _,factory=composite_requisition_app;seed_graph(factory,separate=True)
    with factory() as db:
        snapshot=next(s for s in read_compiled_order_bom(db,1).snapshots if s.component_product_id==2)
        basis=json.loads(product_basis(db.get(Product,2)))
        basis['quantity_basis']={'schema':1,'ledger':'physical','physical_unit':'片','product_id':2,'customer_id':1}
        lot=SimpleNamespace(inventory_type="finished",finished_detail=SimpleNamespace(owner_customer_id=1,physical_basis_json=json.dumps(basis)))
        assert matching_component_basis(db,snapshot,lot)
        basis['unit']='张';lot.finished_detail.physical_basis_json=json.dumps(basis)
        assert not matching_component_basis(db,snapshot,lot)

@pytest.mark.parametrize('die_cut',[True,False])
def test_inbound_identity_uses_same_explicit_cutting_flag_as_compiler(composite_requisition_app,_p181_published_map_identity,die_cut):
    from app.services.multilevel_bom_compile import compile_master_order_bom
    _,factory=composite_requisition_app;seed_graph(factory,two_piece=True)
    with factory() as db:
        p=db.get(Product,2)
        p.box_category='normal' if die_cut else 'die_cut'
        p.sheet_cutting_settings={'schema_version':2,'whole':{'length_parts':1,'width_parts':1,'mold_count':1,'is_die_cut':die_cut}}
        compiled=compile_master_order_bom(db,db.get(OrderItem,1))
        assert product_basis(p)==compiled_product_bases(compiled)[2]

@pytest.mark.parametrize('field,value',[('product_id',999),('unit','张'),('spec','1×2mm'),('material','OTHER'),('flute','X'),('production_process','新增印刷'),('crease_middle_mm','999'),('pieces_per_box','7'),('mold_tool_id','999'),('assembly',[{'product_id':999}])])
def test_real_physical_changes_remain_rejected(composite_requisition_app,_p181_published_map_identity,field,value):
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    _,factory=composite_requisition_app;seed_graph(factory,separate=True)
    with factory() as db:
        snapshot=next(s for s in read_compiled_order_bom(db,1).snapshots if s.component_product_id==2)
        basis=json.loads(product_basis(db.get(Product,2)));basis[field]=value
        lot=SimpleNamespace(inventory_type='finished',finished_detail=SimpleNamespace(physical_basis_json=json.dumps(basis)))
        assert not matching_component_basis(db,snapshot,lot)

@pytest.mark.parametrize('actual,message',[(None,'未冻结'),('null','格式无效'),('[]','格式无效'),('invalid','格式无效'),('{"unit":"张"}','单位：库存 张 / 订单 片')])
def test_identity_message_identifies_batch_and_field(actual,message):
    from app.services.finished_stock_identity import stock_identity_error
    lot=SimpleNamespace(lot_number='FICTIONAL-BATCH-1',finished_detail=SimpleNamespace(inventory_code_snapshot='FICTIONAL-SKU',product_id=1,physical_basis_json=actual))
    error=stock_identity_error(lot,'{"unit":"片"}')
    assert 'FICTIONAL-BATCH-1' in error and 'FICTIONAL-SKU' in error and message in error


def stock_fixture(factory):
    from app.models.material import Material
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.core.time_contract import beijing_today
    from decimal import Decimal
    seed_graph(factory,separate=True)
    with factory() as db:
        product=db.get(Product,2);material=db.get(Material,product.material_id)
        material.quote_price=Decimal('2');material.price_unit='元/㎡';material.purchase_currency='CNY';material.purchase_tax_included=True
        target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=2)
        lot=manual_finished_in(db,customer_id=1,product_id=2,location_id=target.location.id,quantity=20,
            stock_date=beijing_today(),source_type='manual',remarks='隔离虚构库存',operator_id=1,
            idempotency_key='fictional-query-in',expected_layout_version=target.layout_version)
        db.commit()
        return lot.id

@pytest.mark.parametrize('actual',['null','[]','{"quantity_basis":"bad"}'])
def test_authenticated_stock_lookup_reads_old_invalid_json_without_writing(composite_requisition_app,_p181_published_map_identity,actual):
    from fastapi.testclient import TestClient
    from app.models.warehouse_inventory import InventoryLot,InventoryMovement
    from sqlalchemy import select,func
    from tests.test_n039_composite_bom_requisition import _login
    app,factory=composite_requisition_app;lid=stock_fixture(factory)
    with factory() as db:
        lot=db.get(InventoryLot,lid);lot.finished_detail.physical_basis_json=actual;db.commit()
        version=lot.version;count=db.scalar(select(func.count()).select_from(InventoryMovement))
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client)
        for path in ['/api/warehouse/lots?finished_product_code=COMP-A','/api/warehouse/twin-operations/locate?keyword=COMP-A&search_type=finished']:
            response=client.get(path)
            assert response.status_code==200,response.text
            assert 'COMP-A' in response.text
    with factory() as db:
        lot=db.get(InventoryLot,lid)
        assert (lot.version,lot.quantity_available,lot.finished_detail.physical_basis_json)==(version,20,actual)
        assert db.scalar(select(func.count()).select_from(InventoryMovement))==count


def test_component_candidate_and_reservation_ignore_only_quantity_metadata(composite_requisition_app,_p181_published_map_identity):
    from app.models.warehouse_inventory import InventoryLot
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.warehouse_inventory import finished_inventory_candidates_for_bom_component,reserve_finished_inventory_for_bom_component,WarehouseInventoryError
    _,factory=composite_requisition_app;lid=stock_fixture(factory)
    with factory() as db:
        lot=db.get(InventoryLot,lid);snapshot=next(s for s in read_compiled_order_bom(db,1).snapshots if s.component_product_id==2)
        basis=json.loads(lot.finished_detail.physical_basis_json);basis['quantity_basis']={'schema':1,'ledger':'physical','physical_unit':'片','product_id':2,'customer_id':1}
        lot.finished_detail.physical_basis_json=json.dumps(basis);db.commit()
        assert lid in [x.id for x in finished_inventory_candidates_for_bom_component(db,order_item_id=1,bom_snapshot_id=snapshot.id)]
        args=dict(order_item_id=1,bom_snapshot_id=snapshot.id,inventory_lot_id=lid,quantity=3,expected_version=lot.version,operator_id=1,idempotency_key='fictional-reserve',warning_acknowledged_codes=[])
        reserved=reserve_finished_inventory_for_bom_component(db,**args);db.commit()
        assert lot.quantity_available==17 and lot.quantity_reserved==3
        replay=reserve_finished_inventory_for_bom_component(db,**args)
        assert replay.id==reserved.id and lot.quantity_reserved==3
        basis['unit']='张';lot.finished_detail.physical_basis_json=json.dumps(basis);db.commit()
        assert not finished_inventory_candidates_for_bom_component(db,order_item_id=1,bom_snapshot_id=snapshot.id)
        with pytest.raises(WarehouseInventoryError,match='单位：库存 张 / 订单 片'):
            reserve_finished_inventory_for_bom_component(db,**{**args,'idempotency_key':'fictional-mismatch','expected_version':lot.version})
        assert lot.quantity_available==17 and lot.quantity_reserved==3

@pytest.mark.parametrize('marker',[None,[],{'ledger':'physical','physical_unit':'片'}, {'schema':1,'ledger':'physical','physical_unit':'张','product_id':2,'customer_id':1},{'schema':1,'ledger':'physical','physical_unit':'片','product_id':999,'customer_id':1},{'schema':1,'ledger':'physical','physical_unit':'片','product_id':2,'customer_id':999}])
def test_bad_quantity_marker_does_not_become_eligible(composite_requisition_app,_p181_published_map_identity,marker):
    from app.services.finished_stock_identity import matches_component_stock_identity
    _,factory=composite_requisition_app;lid=stock_fixture(factory)
    from app.models.warehouse_inventory import InventoryLot
    with factory() as db:
        lot=db.get(InventoryLot,lid);expected=product_basis(db.get(Product,2));value=json.loads(expected);value['quantity_basis']=marker
        lot.finished_detail.physical_basis_json=json.dumps(value)
        assert not matches_component_stock_identity(lot,expected)
        assert lot.quantity_available==20 and lot.quantity_reserved==0
