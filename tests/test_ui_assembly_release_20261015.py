"""Real component contracts and user-visible selection/preview regressions."""
from datetime import date
import json
from pathlib import Path
import pytest
from sqlalchemy import select
from tests.test_inventory_cost_snapshot import db
from tests.test_bom_entry_cost import seed
from tests.test_p1_09c_104_finished_reservation_action_guard import _method_contents, _run_node
from app.models.warehouse_inventory import InventoryLot
from app.services.warehouse_inventory import manual_finished_in


def finished_children(db):
    parent,children,material,location=seed(db)
    lots=[]
    for index,child in enumerate(children):
        child.length_mm=100;child.width_mm=100;child.height_mm=10
        child.production_process='无需结合'
        db.flush()
        lots.append(manual_finished_in(db,customer_id=parent.customer_id,product_id=child.id,
            location_id=location.id,quantity=[10,18,2][index],stock_date=date(2026,10,15),
            source_type='stocktake',remarks='isolated component',operator_id=None,idempotency_key=f'finished-child-{index}'))
    db.commit()
    return parent,children,lots,location


def test_finished_components_enter_same_strict_preview_and_missing_identity_stays_visible(db):
    from app.services.stock_preparation_assembly import preview
    from app.services.bom_stock_pending import pending_stock
    parent,children,lots,_=finished_children(db)
    p=preview(db,parent.id,1)
    assert p['available_sets']==2
    assert {r['lot_id'] for r in p['sources']}=={l.id for l in lots}
    lots[0].finished_detail.physical_basis_json=None;db.flush()
    p=preview(db,parent.id,1)
    assert p['available_sets']==0
    excluded=next(r for r in p['excluded_sources'] if r.get('lot_id')==lots[0].id)
    assert '未冻结' in excluded['reason'] and excluded['repair_action']=='review_finished_identity'
    row=next(r for r in pending_stock(db,None) if r['parent_product_id']==parent.id)
    assert row['reference_available_sets']==2 and row['available_sets']==0
    assert children[0].product_name in row['assembly_block']


def test_finished_candidates_exclude_other_customer_wrong_basis_and_inactive_location(db):
    from app.services.stock_preparation_assembly import preview
    parent,children,lots,location=finished_children(db)
    from app.models.customer import Customer
    other=Customer(customer_number=9991,customer_code='OTHER-OWNER',name='Other',payment_term_days=0,credit_limit=0)
    db.add(other);db.flush()
    lots[0].finished_detail.owner_customer_id=other.id
    assert preview(db,parent.id,1)['available_sets']==0
    lots[0].finished_detail.owner_customer_id=parent.customer_id
    basis=json.loads(lots[0].finished_detail.physical_basis_json);basis['spec']='different'
    lots[0].finished_detail.physical_basis_json=json.dumps(basis)
    assert preview(db,parent.id,1)['available_sets']==0
    location.is_active=False
    assert preview(db,parent.id,1)['sources']==[]


def test_retired_intermediate_not_normal_candidate_but_active_parent_error_is_visible(db):
    from app.models.product_bom import ProductBomComponent
    from app.models.multilevel_bom import ProductBomProfile,ProductBomInventoryRelation
    from app.services.bom_stock_pending import pending_stock
    parent,children,lots,_=finished_children(db)
    child=children[0];child.is_composite=True;child.is_active=False
    db.add(ProductBomProfile(product_id=child.id,source='assembled',delivery_mode='parent',material_mode='expand_children'))
    edge=ProductBomComponent(parent_product_id=child.id,component_product_id=children[1].id,
        quantity_per_set=1,display_order=1,internal_component_code='intermediate-child',is_required=True)
    db.add(edge);db.flush();db.add(ProductBomInventoryRelation(bom_component_id=edge.id,relation='assembly'));db.flush()
    rows=pending_stock(db,None)
    assert not any(r['parent_product_id']==child.id for r in rows)
    row=next(r for r in rows if r['parent_product_id']==parent.id)
    assert '已停用' in row['error'] and child.product_name in row['error']


def test_customer_switch_cancel_and_same_customer_preserve_all_draft_rows(tmp_path):
    body=_method_contents('chooseOrderCustomer')
    _run_node("const change=new (Object.getPrototypeOf(async function(){}).constructor)('value',"+json.dumps(body)+");"+"""
    (async()=>{
      const row={product_id:9,quantity:10};let resets=0;
      const vm={orderForm:{customer_id:1,items:[row]},handleOrderCustomerChange:async()=>{resets++;}};
      globalThis.confirm=()=>{throw Error('same customer asked');};await change.call(vm,1);
      globalThis.confirm=()=>false;await change.call(vm,2);
      if(vm.orderForm.customer_id!==1||vm.orderForm.items[0]!==row||resets)throw Error('cancel lost draft');
      globalThis.confirm=()=>true;await change.call(vm,2);
      if(vm.orderForm.customer_id!==2||vm.orderForm.items.length||resets!==1)throw Error('switch not atomic');
    })().catch(e=>{console.error(e);process.exit(1)});
    """,tmp_path,'customer-switch.js')


def test_search_edit_focus_and_escape_do_not_clear_selected_identity(tmp_path):
    html=Path('static/index.html').read_text();source=html.split('app.component("search-select", {',1)[1].split('      window.ERPOrderContext',1)[0]
    _run_node('let definition;const app={component:(name,body)=>definition=body};app.component("search-select",{'+source+"""
    const events=[];const vm={disabled:false,modelValue:7,query:'selected',selectedLabel:'selected',timer:null,$emit:(...args)=>events.push(args),requestPinyinRuntime(){}};
    definition.methods.onFocus.call(vm);vm.query='different';definition.methods.onInput.call(vm);definition.methods.onBlur.call(vm);clearTimeout(vm.timer);
    if(events.some(e=>e[0]==='update:modelValue')||vm.query!=='selected'||vm.open)throw Error('search/focus changed selected identity');
    """,tmp_path,'search-selection.js')


def test_common_box_available_stock_does_not_subtract_reserved_twice_and_keeps_bad_bom_visible(db):
    from app.api.products import list_products
    from app.models.user import User
    parent,children,lots,_=finished_children(db)
    lots[0].quantity_available=7;lots[0].quantity_reserved=3;db.flush()
    actor=User(id=999,username='isolated-reader',real_name='UAT',role='admin',is_active=True,customer_access_mode='all')
    args=dict(customer_id=parent.customer_id,response_mode='summary',selection_context='order',sort_by='code',page=1,page_size=25,include_inventory=True,db=db,user=actor)
    result=list_products(**args);items={r['id']:r for r in result['items']}
    assert items[children[0].id]['available_finished_quantity']==7
    children[0].is_active=False;db.flush()
    result=list_products(**args);row=next(r for r in result['items'] if r['id']==parent.id)
    assert '已停用' in row['inventory_error'] and 'available_finished_quantity' not in row


def test_mac_uat_process_identity_remains_strict(monkeypatch):
    import os,sys,psutil
    from app.core import uat_isolation as uat
    monkeypatch.setattr(uat.sys,'platform','darwin')
    token=uat.process_creation_token(os.getpid())
    assert token.startswith('darwin:') and token==uat.process_creation_token(os.getpid())
    assert uat.process_creation_token(999999999) is None
    def inaccessible(pid):raise psutil.AccessDenied(pid)
    monkeypatch.setattr(psutil,'Process',inaccessible)
    with pytest.raises(uat.UatIsolationError,match='Cannot read process identity'):
        uat.process_creation_token(os.getpid())

from tests.test_phase11_requisition import requisition_app

def test_order_material_change_is_order_only_unless_master_sync_explicit(requisition_app):
    from fastapi.testclient import TestClient
    from tests.test_phase11_requisition import _login
    from app.api.orders import router
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.product import Product
    app,factory=requisition_app;app.include_router(router,prefix='/api/orders')
    with factory() as db:
        material=Material(code='ORDER-ONLY-MAT',supplier_name='鸣朋',layer_count=3,flute_type='B',quote_price=2,
            price_unit='元/㎡',purchase_currency='CNY',purchase_tax_included=True,purchase_tax_rate='.13')
        db.add(material);db.commit();mid=material.id
        item=db.get(OrderItem,1);product=db.get(Product,item.product_id)
        baseline=(product.material_id,product.version,product.legacy_material_text)
    body=dict(quantity=100,unit_price='3.60',product_code='21301028',product_name='中性外箱',
        specification='520×350×300mm',material_id=mid,material='ORDER-ONLY-MAT',layer_count=3,flute_type='B',sync_product=False)
    with TestClient(app) as client:
        _login(client,'admin')
        response=client.put('/api/orders/items/1',json=body)
        assert response.status_code==200,response.text
        with factory() as db:
            item=db.get(OrderItem,1);product=db.get(Product,item.product_id)
            assert item.material_id==mid
            assert (product.material_id,product.version,product.legacy_material_text)==baseline
        body['sync_product']=True
        response=client.put('/api/orders/items/1',json=body)
        assert response.status_code==400 and 'product_expected_version' in response.text
