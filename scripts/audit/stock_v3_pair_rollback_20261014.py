"""Owned child-only live-lot reserve/release audit. Every case fully rolls back."""
from pathlib import Path
import collections, hashlib, json, math, os, sqlite3, sys
from datetime import date
from decimal import Decimal
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
BASELINE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/managed-tasks/task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3')
EVIDENCE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol')
CHILD=Path(os.environ['ERP_DATABASE_PATH']).resolve()
assert CHILD!=BASELINE.resolve() and CHILD.parent.name=='copies'
assert os.environ.get('ERP_ENVIRONMENT')=='test'
assert os.environ.get('ERP_UAT_ISOLATION_ID')
assert CHILD==Path(os.environ['ERP_UAT_ROOT']).resolve()/'carton_erp.sqlite3'
assert CHILD.parent.parent.joinpath('result.json').exists()
assert json.loads(CHILD.parent.parent.joinpath('result.json').read_text())['source']==str(BASELINE)
# This warehouse test does not claim customer-document extraction coverage.
# New child orders use their stored product fields and retained DB remarks,
# while the evidence reader remains isolated from source checkout files.
CHILD.parent.joinpath('customer-document-review.json').write_text('{"entries":[]}')

def file_hash(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
base_before=file_hash(BASELINE)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from pydantic import ValidationError
from app.models import Base
from app.models.product import Product
from app.models.order import Order,OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot,OrderItemSemiRequirement,InventoryReservation
from app.services import semi_finished_inventory as semi
from app.services.warehouse_inventory import WarehouseInventoryError
from app.services.product_specification import product_dimension_specification
from app.services.requisition_quantities import purchase_sheet_quantity,normalize_cutting_mode
from app.services.sheet_cutting_settings import component_settings
from app.api.warehouse import SemiRequirementPayload

def connect():
 c=sqlite3.connect(str(CHILD));c.execute('pragma foreign_keys=on');return c
engine=create_engine('sqlite://',creator=connect)
raw=connect();raw.row_factory=sqlite3.Row
all_tables=[r[0] for r in raw.execute("select name from sqlite_master where type='table' order by name")]
mutable_tables=['inventory_lots','semi_finished_inventory_details','warehouse_goods_profiles','products',
 'sales_orders','sales_order_items','order_item_semi_requirements','inventory_reservations','inventory_movements',
 'semi_finished_match_rules','semi_finished_match_rule_products','production_tasks','operation_logs','sqlite_sequence']
mutable_tables=[t for t in mutable_tables if t in all_tables]

def logical_hash(tables):
 h=hashlib.sha256()
 for t in tables:
  h.update(t.encode());rows=raw.execute('SELECT * FROM "'+t+'" ORDER BY '+','.join(str(i+1) for i in range(len(raw.execute('pragma table_info("'+t+'")').fetchall()))))
  for row in rows:h.update(json.dumps(tuple(row),default=str,ensure_ascii=False,separators=(',',':')).encode());h.update(b'\n')
 return h.hexdigest()

def lot_facts(db,lot_id):
 lot=db.get(InventoryLot,lot_id)
 return {'lot':{c.name:getattr(lot,c.name) for c in InventoryLot.__table__.columns},
  'detail':{c.name:getattr(lot.semi_finished_detail,c.name) for c in lot.semi_finished_detail.__table__.columns}}

def inventory_facts(v):
 return {k:v['lot'][k] for k in ('quantity_available','quantity_reserved','quantity_consumed','quantity_damaged','quantity_scrapped','unit')}

before_all=logical_hash(all_tables);before_mutable=logical_hash(mutable_tables)
schema_before=[tuple(r) for r in raw.execute('select type,name,sql from sqlite_master order by type,name')]
audit=json.loads((EVIDENCE/'all-live-stock-evidence.json').read_text())
assert audit['database_sha256_before']==base_before
pairs=[(r,p) for r in audit['semi_rows'] for p in r['qualification_pairs']]
assert len(pairs)==33
results=[];fatal=[];fault_tested=False
with Session(engine,autoflush=False,expire_on_commit=False) as session:
 actor=session.scalar(select(User.id).where(User.role=='admin',User.is_active.is_(True)).order_by(User.id));assert actor
 session.rollback()

for n,(record,pair) in enumerate(pairs):
 result={'lot_id':record['lot_id'],'product_id':pair['product_id'],'stock_unit':record['unit'],
  'read_only_qualification':pair,'reservation_exercised':False,'release_exercised':False,'demand_requisition_gap_unit':'pieces/sheets'}
 result['candidate']={'returned':None,'not_executed_reason':'需求payload尚未验证'}
 result['requirement_created']=False
 try:
  with Session(engine,autoflush=False,expire_on_commit=False) as db:
   db.connection().exec_driver_sql('BEGIN')
   try:
    initial=lot_facts(db,record['lot_id']);p=db.get(Product,pair['product_id']);lot=db.get(InventoryLot,record['lot_id'])
    component=lot.semi_finished_detail.component_type
    length=p.base_report_length_mm if component=='base' else p.report_length_mm
    width=p.base_report_width_mm if component=='base' else p.report_width_mm
    material=(p.material.code if p.material else p.default_material_code or p.legacy_material_text) or ''
    pieces=1 if component in ('cover','base') else int(p.pieces_per_box or 1)
    output=semi.theoretical_product_yield(p,component)
    finished_quantity=max(10,math.ceil(2*output/pieces));required=finished_quantity*pieces
    SemiRequirementPayload(component_type=component,board_length_mm=length,board_width_mm=width,
     material_code=material,flute_type=p.flute_type,pieces_per_box=pieces,
     stock_yield_per_sheet=output,required_piece_quantity=required)
    order=Order(order_number='GOVERNANCE-CHILD-'+str(n),customer_id=p.customer_id,
     order_date=date.today(),status='pending_production',payment_status='unpaid',total_amount=Decimal(0),created_by=actor)
    db.add(order);db.flush()
    item=OrderItem(order_id=order.id,product_id=p.id,quantity=finished_quantity,unit_price=Decimal(0),subtotal=Decimal(0),
     material_status='pending',requisition_status='未报料',snapshot_product_code=p.product_code,snapshot_product_name=p.product_name,
     snapshot_spec=product_dimension_specification(p),snapshot_material=material,material_id=p.material_id,
     layer_count=p.layer_count,flute_type=p.flute_type,snapshot_report_length_mm=length,snapshot_report_width_mm=width,
     snapshot_splice_mode=p.splice_mode,snapshot_pieces_per_box=pieces, snapshot_production_notes=p.production_notes,
     snapshot_crease_type=p.crease_type,snapshot_crease_left_mm=p.crease_left_mm,
     snapshot_crease_middle_mm=p.crease_middle_mm,snapshot_crease_right_mm=p.crease_right_mm,
     snapshot_base_crease_type=p.base_crease_type,snapshot_base_crease_left_mm=p.base_crease_left_mm,
     snapshot_base_crease_middle_mm=p.base_crease_middle_mm,snapshot_base_crease_right_mm=p.base_crease_right_mm,
     sheet_cutting_settings_snapshot=p.sheet_cutting_settings,special_process=p.default_cutting_mode,
     supply_mode_snapshot=p.supply_mode,sales_unit_snapshot=p.unit)
    db.add(item);db.flush()
    requirement=semi.save_order_item_semi_requirement(db,order_item_id=item.id,component_type=component,
     board_length_mm=length,board_width_mm=width,material_code=material,flute_type=p.flute_type,
     pieces_per_box=pieces,stock_yield_per_sheet=output,required_piece_quantity=required,operator_id=actor)
    result['requirement_created']=True
    setting=component_settings(p.sheet_cutting_settings,component)
    supplier_yield=setting.contract(length,width).yield_per_supplier_sheet if setting else output
    mode=normalize_cutting_mode(supplier_yield,strict=True)
    def gap():
     credited=semi.active_semi_requirement_credited_quantity(db,requirement.id)
     return {'required_piece_quantity':requirement.required_piece_quantity,'reserved_credit_piece_quantity':credited,
      'remaining_piece_quantity':required-credited,'purchase_sheet_quantity':purchase_sheet_quantity(required,credited,mode),
      'supplier_yield_per_sheet':supplier_yield}
    result['requirement_before']=gap()
    candidates=semi.semi_finished_inventory_candidates(db,requirement.id)
    candidate=next((c for c in candidates if c.lot.id==lot.id),None)
    result['candidate']={'returned':candidate is not None,'selectable':candidate.selectable if candidate else False,
     'direct_deduction_eligible':candidate.direct_deduction_eligible if candidate else False,
     'warnings':list(candidate.warning_codes) if candidate else [],'signature_differences':list(candidate.signature_differences) if candidate else []}
    expected=lot.version;key='gov-reserve-'+str(n)
    # No reviewed/spec override is invented. Standard accepted bindings are
    # exercised, and the returned business rejection is retained for the rest.
    kwargs=dict(requirement_id=requirement.id,requested_requirement_quantity=output,
     lots=[semi.SemiFinishedLotVersion(lot.id,expected)],operator_id=actor,
     idempotency_key=key,confirmed=True,warning_acknowledged_codes=list(candidate.warning_codes) if candidate else [])
    try:batch=semi.reserve_semi_finished_inventory(db,**kwargs)
    except WarehouseInventoryError as error:
     db.expire_all();result['reservation']={'accepted':False,'status_code':error.status_code,'reason':str(error)}
     assert inventory_facts(lot_facts(db,record['lot_id']))==inventory_facts(initial)
     result['requirement_after_rejection']=gap();assert result['requirement_after_rejection']==result['requirement_before']
    else:
     result['reservation_exercised']=True;result['reservation']={'accepted':True,'reservation_ids':[r.id for r in batch.reservations]}
     assert len(batch.reservations)==1
     reservation=batch.reservations[0];db.expire_all();reserved=lot_facts(db,record['lot_id'])
     result['after_reserve_balance']=inventory_facts(reserved);result['requirement_after_reserve']=gap()
     q=reservation.reserved_stock_quantity;credit=reservation.credited_requirement_quantity
     assert reserved['lot']['quantity_available']==initial['lot']['quantity_available']-q
     assert reserved['lot']['quantity_reserved']==initial['lot']['quantity_reserved']+q
     assert reserved['lot']['quantity_consumed']==initial['lot']['quantity_consumed']
     assert credit<=q*reservation.yield_factor and result['requirement_after_reserve']['reserved_credit_piece_quantity']==credit
     replay=semi.reserve_semi_finished_inventory(db,**kwargs)
     assert [r.id for r in replay.reservations]==[reservation.id]
     result['same_key_replay']=True
     for label,changes in [('different_quantity',{'requested_requirement_quantity':output+1}),
                           ('stale_version',{'idempotency_key':key+'-stale','lots':[semi.SemiFinishedLotVersion(lot.id,expected)]})]:
      try:semi.reserve_semi_finished_inventory(db,**{**kwargs,**changes})
      except WarehouseInventoryError as error:result[label]={'rejected':True,'status_code':error.status_code,'reason':str(error)}
      else:raise AssertionError(label+' did not reject')
     db.expire_all();current_lot=db.get(InventoryLot,lot.id)
     semi.release_semi_finished_reservation(db,reservation_id=reservation.id,expected_version=current_lot.version,
      operator_id=actor,release_reason='managed child governance audit',idempotency_key='gov-release-'+str(n))
     db.expire_all();released=lot_facts(db,record['lot_id']);result['release_exercised']=True
     result['after_release_balance']=inventory_facts(released);result['requirement_after_release']=gap()
     assert inventory_facts(released)==inventory_facts(initial)
     assert result['requirement_after_release']==result['requirement_before']
     for field in initial['lot']:
      if field not in ('version','last_movement_at','updated_at'):assert released['lot'][field]==initial['lot'][field],field
     assert released['detail']==initial['detail']
     if not fault_tested:
      after_release=lot_facts(db,record['lot_id'])
      movement=semi._movement
      def fail_after_movement(*args,**arguments):
       value=movement(*args,**arguments)
       if arguments.get('movement_type')=='reserve':raise RuntimeError('owned-child-reserve-fault')
       return value
      semi._movement=fail_after_movement
      try:
       try:semi.reserve_semi_finished_inventory(db,**{**kwargs,'idempotency_key':key+'-fault',
        'lots':[semi.SemiFinishedLotVersion(lot.id,after_release['lot']['version'])]})
       except RuntimeError as error:assert str(error)=='owned-child-reserve-fault'
       else:raise AssertionError('fault injection did not run')
      finally:semi._movement=movement
      db.expire_all();assert lot_facts(db,record['lot_id'])==after_release
      assert gap()==result['requirement_before']
      result['post_movement_fault_rollback_equal']=True;fault_tested=True
   except ValidationError as error:
    result['business_rejection']={'status_code':422,'reason':'真实需求payload缺合法尺寸等字段',
     'invalid_fields':error.errors(include_url=False,include_input=False)}
   except WarehouseInventoryError as error:result['business_rejection']={'status_code':error.status_code,'reason':str(error)}
   finally:db.rollback()
  assert logical_hash(mutable_tables)==before_mutable
  result['per_case_table_rollback_equal']=True
  result['requirement_table_rollback_equal']=True
 except Exception as error:
  result['fatal']={'type':type(error).__name__,'reason':str(error)};fatal.append(result)
 results.append(result)
 (EVIDENCE/'managed-pair-progress.json').write_text(json.dumps({'results':results,'fatal':fatal},ensure_ascii=False,indent=2,default=str))
 if fatal:break

final_hash=logical_hash(all_tables)
assert final_hash==before_all
assert schema_before==[tuple(r) for r in raw.execute('select type,name,sql from sqlite_master order by type,name')]
raw.close();engine.dispose();base_after=file_hash(BASELINE);assert base_after==base_before
summary={'scope':'33 existing live semi-lot/target-product pairs; reserve/release only, no consumption',
 'child':str(CHILD),'baseline_unchanged':base_before==base_after,'baseline_sha256':base_before,
 'logical_fingerprint_before':before_all,'logical_fingerprint_after':final_hash,
 'pairs_completed':len(results),'reservation_release_passed':sum(r['release_exercised'] for r in results),
 'post_movement_fault_rollback_tested':fault_tested,
 'business_rejections':sum(bool(r.get('business_rejection') or r.get('reservation',{}).get('accepted') is False) for r in results),
 'fatal':fatal,'results':results,'service_controlled':False,'child_committed':False}
(EVIDENCE/'managed-pair-results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=str))
print(json.dumps({k:v for k,v in summary.items() if k not in ('results','fatal')},ensure_ascii=False))
if fatal:sys.exit(1)
