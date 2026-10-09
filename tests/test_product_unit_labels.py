import json
from types import SimpleNamespace

import pytest

from app.services.product_unit_labels import product_unit_info, product_unit_label, basis_unit_label, order_unit_label
from app.services.warehouse_display_units import lot_display_unit
from tests.test_finished_goods_inventory_reservation import reservation_db, add_lot


CASES = [
    ({"unit":"只","production_process":"无需结合"}, "片", False),
    ({"unit":"只","production_process":"模切、无需结合"}, "片", False),
    ({"unit":"片","production_process":"粘贴,模切,二次粘合"}, "只", False),
    ({"unit":"片","production_process":"开槽,印刷,打钉"}, "只", False),
    ({"unit":"片","production_process":"粘贴,打钉"}, "只", False),
    ({"unit":"只","is_internal_component":True,"production_process":"粘贴"}, "片", False),
    ({"unit":"只","is_composite":True,"production_process":"粘贴"}, "套", False),
    ({"unit":"套","is_composite":True,"is_internal_component":True}, "套", False),
    ({"unit":"只","box_style":"BOM组合"}, "套", False),
    ({"unit":"只","production_process":None}, "只", True),
    ({"unit":"片","production_process":"模切"}, "片", True),
    ({"unit":"只","production_process":"无需结合,粘贴"}, "只", True),
    ({"unit":"根","supply_mode":"external_purchase","is_internal_component":True}, "根", False),
    ({"unit":"公斤","production_process":"无需结合"}, "公斤", False),
    ({"unit":"张","production_process":"无需结合"}, "张", False),
]


@pytest.mark.parametrize("facts,label,review", CASES)
def test_business_unit_rules_preserve_inputs(facts,label,review):
    before=json.dumps(facts,ensure_ascii=False)
    assert product_unit_info(facts)==dict(unit_label=label,unit_needs_review=review)
    assert json.dumps(facts,ensure_ascii=False)==before


def test_frozen_stock_and_sales_do_not_read_changed_master():
    product=SimpleNamespace(unit='只',production_process='粘贴',is_composite=False,is_internal_component=False)
    basis=dict(unit='只',production_process='无需结合',assembly=[])
    frozen=json.dumps(basis)
    lot=SimpleNamespace(source_ref_type='manual',unit='boxes',finished_detail=SimpleNamespace(
        product=product,physical_basis_json=frozen))
    assert lot_display_unit(lot)=='片'
    assert lot.finished_detail.physical_basis_json==frozen and lot.unit=='boxes'
    assert basis_unit_label({**basis,'assembly':[{'product_id':1}]})=='套'
    assert basis_unit_label({**basis,'assembly':[{'product_id':1}],'inventory_stage':'body'})=='片'
    assert basis_unit_label({**basis,'quantity_basis':{'ledger':'physical','physical_unit':'只'}})=='只'
    assert order_unit_label(SimpleNamespace(sales_unit_snapshot='套'),product)=='套'
    assert order_unit_label(SimpleNamespace(sales_unit_snapshot=None),product)=='只'


@pytest.mark.parametrize('sales_unit,eligible',[('片',True),('只',True),('套',False),('张',False)])
def test_renamed_new_order_preserves_existing_shared_stock_contract(reservation_db,sales_unit,eligible):
    from app.models.order import OrderItem
    from app.models.shared_finished_stock import SharedFinishedOrderBasis
    from app.services import shared_finished_stock as shared
    from app.services.shared_finished_receipts import PAIRS
    from app.services.box_type_rules import order_snapshot_box_configuration
    from tests.test_shared_finished_stock import setup_pair,activate,reserve_for
    from tests.test_shared_finished_management import configure
    db,data=reservation_db
    target,other,_=setup_pair(db,data)
    product=data['product']; product.production_process=target.production_process='无需结合'
    db.flush();lot=add_lot(db,data,quantity=25,key='named-stock');db.commit()
    gid=activate(db,data,target,lot)['group_id'];configure(db,data,gid);db.commit()
    identity=shared.lot_identity(lot); member=shared.member_identity(product)
    basis=json.loads(shared.product_basis(product))
    item=OrderItem(order_id=data['order'].id,product_id=product.id,quantity=20,
        unit_price=2,subtotal=40,material_status='pending',requisition_status='未报料',
        snapshot_product_code=product.product_code,snapshot_product_name=product.product_name,
        snapshot_spec=basis['spec'],supply_mode_snapshot='corrugated_production',combination_role='standalone')
    for field,key in PAIRS.items():setattr(item,field,basis.get(key))
    config=order_snapshot_box_configuration(product)
    for key in ('splice_mode','pieces_per_box','flap_mm'):setattr(item,'snapshot_'+key,config[key])
    item.special_process=config['default_cutting_mode'];item.sales_unit_snapshot=sales_unit
    for field in ('report_length_mm','report_width_mm','base_report_length_mm','base_report_width_mm'):
        setattr(item,'snapshot_'+field,getattr(product,field))
    item.sheet_cutting_settings_snapshot=product.sheet_cutting_settings
    db.add(item);db.flush()
    assert (db.get(SharedFinishedOrderBasis,item.id) is not None)==eligible
    assert lot_display_unit(lot)=='片'
    assert product.unit=='只' and shared.member_identity(product)==member
    assert shared.lot_identity(lot)==identity
    reserve_for(db,data,other,lot,10,'rename-reserve');db.commit()
    assert (lot.quantity_available,lot.quantity_reserved)==(15,10)
    assert shared.lot_identity(lot)==identity


def test_new_product_uses_standard_name_and_old_master_response_keeps_identity(reservation_db):
    from app.api.products import ProductPayload,_create_product,_response
    db,data=reservation_db;p=data['product'];p.production_process='无需结合';db.commit()
    original_unit=p.unit
    result=_response(p,data['admin'])
    assert result['unit']==original_unit and result['unit_label']=='片' and not result['unit_needs_review']
    payload=ProductPayload(customer_id=data['customer'].id,product_code='UNIT-NEW',customer_material_code='UNIT-NEW',product_name='虚构平片',
        box_category='normal',length_mm=800,width_mm=200,height_mm=100,production_process='无需结合',
        material_id=p.material_id,flute_type='BE',layer_count=5)
    created=_create_product(payload,db,data['admin'])
    assert created['unit']==created['unit_label']=='片'
    db.expire_all()
    from app.models.product import Product
    assert db.get(Product,created['id']).unit=='片' and db.get(Product,p.id).unit==original_unit


def test_browser_form_rules_agree_with_backend():
    import subprocess
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    script="""
const api=require('./static/js/product-unit-labels.js');
let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{
 const rows=JSON.parse(input);for(const [facts,label,review] of rows){
  const result=api.info(facts,true);
  if(result.label!==label || result.review!==review) throw Error(JSON.stringify({facts,result,label,review}));
 }
 const edited=api.info({unit:'只',production_process:'粘贴',_production_processes:['无需结合']},true);
 if(edited.label!=='片') throw Error('unsaved process preview stale');
 const unknown={id:1,unit:'只',unit_needs_review:true,production_process:null,_production_processes:['无需结合']};
 if(!api.info(unknown,true).review || api.info(unknown,true).label!=='只') throw Error('unknown process default guessed');
 if(api.info({...unknown,_joining_choice_manual:true},true).label!=='片') throw Error('explicit process ignored');
 if(api.pendingProcess({...unknown,_production_processes:['模切']})!=='模切') throw Error('mold edit lost');
 if(api.pendingProcess({...unknown,_production_processes:[]})!==null) throw Error('empty original changed');
 if(api.pendingProcess({production_process:'开槽,模切',_production_processes:[]})!=='开槽') throw Error('mold removal lost other process');
 console.log('front and backend unit names agree');
});
"""
    result=subprocess.run(['node','-e',script],cwd=root,input=json.dumps(CASES,ensure_ascii=False),
        text=True,encoding='utf8',capture_output=True)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('process', [None, '开槽'])
def test_unrelated_edit_preserves_unknown_process_and_original_unit(reservation_db, process):
    from app.api.products import ProductUpdatePayload, _response, _validated_product_versioned_updates
    db,data=reservation_db;p=data['product'];p.production_process=process;db.commit()
    raw=_response(p,data['admin'])
    payload=ProductUpdatePayload.model_validate({**raw,'expected_version':p.version,'production_notes':'虚构备注'})
    updates=_validated_product_versioned_updates(db,product=p,payload=payload,user=data['admin'])
    assert updates['production_process']==process
    assert updates['unit']==p.unit
    assert product_unit_info(p)['unit_needs_review']


def test_nonjoining_edit_preserves_unknown_joining_and_mold_validation(reservation_db):
    from fastapi import HTTPException
    from app.api.products import ProductUpdatePayload, _response, _validated_product_versioned_updates
    db,data=reservation_db;p=data['product'];p.production_process=None;db.commit()
    raw=_response(p,data['admin'])
    payload=ProductUpdatePayload.model_validate({**raw,'expected_version':p.version,'production_process':'开槽'})
    updates=_validated_product_versioned_updates(db,product=p,payload=payload,user=data['admin'])
    assert updates['production_process']=='开槽'
    mold=ProductUpdatePayload.model_validate({**raw,'expected_version':p.version,'production_process':'模切'})
    with pytest.raises(HTTPException, match='必须选择已登记的生产模具'):
        _validated_product_versioned_updates(db,product=p,payload=mold,user=data['admin'])
    assert mold.production_process=='模切'
    from app.models.mold_tool import MoldTool
    registered=MoldTool(mold_code='UNIT-MOLD',mold_name='虚构模切',rack_location='3F-M-R02-L2-G02')
    db.add(registered);db.flush();mold.mold_tool_id=registered.id
    saved=_validated_product_versioned_updates(db,product=p,payload=mold,user=data['admin'])
    assert saved['production_process']=='模切' and saved['mold_tool_id']==registered.id
