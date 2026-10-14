"""Read-only inventory evidence audit. No writes or production paths opened."""
import ast, collections, datetime, hashlib, json, os, pathlib, re, sqlite3, sys
from types import SimpleNamespace
SOURCE=pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SOURCE));sys.dont_write_bytecode=True
os.environ.update(ERP_ENVIRONMENT='test',ERP_DATABASE_PATH='/private/var/empty/readonly-audit-never-open.sqlite3',ERP_SECRET_KEY='readonly-audit-not-used-for-authentication')
DB=pathlib.Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/managed-tasks/task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3')
OUTPUT=pathlib.Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol')
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
before=digest(DB)
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from app.models import Base
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services.finished_stock_identity import product_basis, matches_stock_identity
from app.services.frozen_assembly_mold_proof import matches_proven_assembly
from app.services.legacy_bom_mold_confirmation import confirmed_match
from app.services.shared_finished_stock import match as shared_match
from app.services.warehouse_inventory import finished_inventory_candidates_for_product
from app.services.delivery_quantities import product_basis as quantity_basis, require_physical_stock
from app.services.inventory_valuation import resolve_product_cost


def connect():
 c=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=2)
 c.execute('PRAGMA query_only=ON');return c
engine=create_engine('sqlite://',creator=connect)
out=dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),query_only=True,business_written=False,service_controlled=False,source=str(SOURCE),database=str(DB),rows=[],semi_rows=[],source_sha256={})
def err(e):return {'type':type(e).__name__,'message':str(e)[:1200]}
def parsed(s):
 try:return json.loads(s) if s else None
 except (ValueError,TypeError):return None
with Session(engine,autoflush=False) as db:
 db.execute(text('BEGIN'));assert db.execute(text('PRAGMA query_only')).scalar()==1
 raw=db.connection().connection.driver_connection;raw.row_factory=sqlite3.Row
 tables={r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 def rows(t,where='1=1',args=()):
  return [dict(r) for r in raw.execute('SELECT * FROM "'+t+'" WHERE '+where,args)] if t in tables else []
 lots=list(db.scalars(select(InventoryLot).where((InventoryLot.quantity_available+InventoryLot.quantity_reserved+InventoryLot.quantity_damaged)>0).order_by(InventoryLot.id)))
 out['live_by_type']=dict(collections.Counter(l.inventory_type for l in lots))
 out['total_lots']=raw.execute('SELECT count(*) FROM inventory_lots').fetchone()[0]
 bases={};candidates={};profiles={r['lot_id']:parsed(r['data_json']) or {} for r in rows('warehouse_goods_profiles')}
 allowed=collections.defaultdict(list)
 for r in rows('semi_finished_lot_allowed_products'):allowed[r['inventory_lot_id']].append(r['product_id'])
 for lot in lots:
     loc=lot.location
     r=dict(lot_id=lot.id,lot_number=lot.lot_number,type=lot.inventory_type,source_type=lot.source_type,
         source_ref_type=lot.source_ref_type,source_ref_id=lot.source_ref_id,available=lot.quantity_available,
         reserved=lot.quantity_reserved,damaged=lot.quantity_damaged,status=lot.status,unit=lot.unit,
         location_id=lot.warehouse_location_id,location_name=loc.location_name if loc else None,
         location_active=bool(loc.is_active) if loc else False,created_at=str(lot.created_at),issues=[])
     if not loc or not loc.is_active:r['issues'].append('inactive_or_missing_location')
     if lot.inventory_type=='finished':
         d=lot.finished_detail;p=d.product if d else None
         r.update(product_id=d.product_id if d else None,code=d.inventory_code_snapshot if d else None,
                  name=d.product_name_snapshot if d else None,customer_id=d.owner_customer_id if d else None)
         if not d or not p:r['identity']='missing_detail_or_product'
         else:
             try:
                 if p.id not in bases:bases[p.id]=product_basis(p)
                 basis=bases[p.id];actual=parsed(d.physical_basis_json)
                 if d.physical_basis_json is None:r['identity']='missing_identity'
                 elif not isinstance(actual,dict):r['identity']='invalid_identity'
                 elif matches_stock_identity(d.physical_basis_json,basis):r['identity']='exact'
                 elif matches_proven_assembly(lot,basis):r['identity']='proven_assembly'
                 elif confirmed_match(lot,basis):r['identity']='explicit_confirmation'
                 elif shared_match(db,lot,product_id=p.id,customer_id=d.owner_customer_id,expected_basis=basis):r['identity']='explicit_shared_confirmation'
                 else:
                     r['identity']='mismatch';expected=json.loads(basis)
                     r['differences']={k:dict(stock=actual.get(k),expected=expected.get(k)) for k in sorted(set(actual)|set(expected)) if k!='quantity_basis' and actual.get(k)!=expected.get(k)}
                 if p.id not in candidates:
                     cs=finished_inventory_candidates_for_product(db,customer_id=p.customer_id,product_id=p.id)
                     qb=quantity_basis(p);valid=[]
                     for cl in cs:
                         try:require_physical_stock(cl,qb);valid.append(cl.id)
                         except ValueError:pass
                     candidates[p.id]=valid
                 r['current_product_candidate']=lot.id in candidates[p.id]
             except Exception as e:r['identity']=r.get('identity','master_basis_error');r['error']=err(e)
         out['rows'].append(r)
     elif lot.inventory_type=='semi_finished':
         d=lot.semi_finished_detail;pr=profiles.get(lot.id,{})
         r.update(profile=pr,allowed_product_ids=allowed[lot.id],
             sheet_type=d.sheet_type if d else None,material=d.material_code_snapshot if d else None,
             component_type=d.component_type if d else None,pieces_per_box=d.pieces_per_box if d else None,
             yield_per_sheet=d.stock_yield_per_sheet if d else None,board_length_mm=d.board_length_mm if d else None,
             board_width_mm=d.board_width_mm if d else None,name=d.internal_name if d else None)
         if not d:r['issues'].append('missing_detail')
         if not pr:r['issues'].append('no_profile')
         if pr.get('processing')=='die_cut' and not (pr.get('product_ids') or allowed[lot.id]) and not (pr.get('mold_tool_id') and pr.get('blank_unprinted')):
             r['issues'].append('processed_usage_unconfirmed')
         if pr.get('output_piece'):
             try:
                 from app.services.processed_component_stock import output_identity,eligible_output
                 identity=output_identity(db,lot)
                 r['completed_identity_valid']=bool(identity)
                 if identity:
                     pid,cid,b=identity;p=db.get(Product,pid)
                     r.update(product_id=pid,code=p.product_code,completed_eligible=eligible_output(db,lot,product_id=pid,customer_id=cid,expected_basis=product_basis(p)))
             except Exception as e:r['error']=err(e)
         out['semi_rows'].append(r)
 # Typed lineage is a separate evidence graph, not a heuristic from lot remarks.
 all_lots={r['id']:r for r in rows('inventory_lots')}
 all_finished={r['inventory_lot_id']:r for r in rows('finished_goods_inventory_details')}
 transfers=rows('inventory_lot_transfers');incoming=collections.defaultdict(list)
 for edge in transfers:incoming[edge['target_lot_id']].append(edge)
 master=collections.defaultdict(list)
 for version in rows('master_data_object_versions',"object_type='product'"):
  version['snapshot']=parsed(version.pop('snapshot_json')) or {};master[version['object_id']].append(version)
 for versions in master.values():versions.sort(key=lambda v:(v['created_at'],v['id']))
 movements=collections.defaultdict(list)
 for event in rows('inventory_movements'):movements[event['inventory_lot_id']].append(event)
 completions=collections.defaultdict(list)
 for event in rows('production_completions'):completions[event['inventory_lot_id']].append(event)
 for event in rows('production_stock_transfers'):completions[event['inventory_lot_id']].append(event)
 stocktakes=collections.defaultdict(list)
 for event in rows('stocktake_items'):stocktakes[event['inventory_lot_id']].append(event)
 preparation=collections.defaultdict(list)
 for event in rows('stock_preparation_jobs'):preparation[event['output_lot_id']].append(event)
 assemblies=collections.defaultdict(list)
 for event in rows('bom_assemblies'):assemblies[event['output_lot_id']].append(event)
 reference_tables={'production_completion':'production_completions','incoming_receipt_item':'incoming_receipt_items',
  'external_packaging_receipt_item':'external_packaging_receipt_items','return_receipt_item':'return_receipt_items',
  'stock_preparation':'stock_preparation_jobs','bom_assembly':'bom_assemblies',
  'stock_replenishment_receipt':'incoming_receipt_items'}
 def lineage(lot_id, trail=()):
  if lot_id in trail:return {'root_ids':[],'edges':[],'issues':['typed_transfer_cycle']}
  moves=[e for e in incoming[lot_id] if e['source_lot_id']==lot_id]
  links=[e for e in incoming[lot_id] if e['source_lot_id']!=lot_id]
  if not links:return {'root_ids':[lot_id],'edges':moves,'issues':[]}
  result={'root_ids':[],'edges':moves,'issues':[]}
  for edge in links:
   if edge['source_lot_id'] not in all_lots:result['issues'].append('typed_source_lot_missing');continue
   branch=lineage(edge['source_lot_id'],trail+(lot_id,))
   result['root_ids']+=branch['root_ids'];result['edges']+=branch['edges']+[edge];result['issues']+=branch['issues']
  result['root_ids']=sorted(set(result['root_ids']))
  if len(result['root_ids'])>1:result['issues'].append('multiple_typed_transfer_roots')
  return result
 def evidence(lot_id):
  l=all_lots[lot_id];table=reference_tables.get(l['source_ref_type'])
  ref=rows(table,'id=?',(l['source_ref_id'],)) if table and l['source_ref_id'] is not None else []
  # stock_replenishment_receipt can name a reserve item; preserve ambiguity.
  return {'lot_id':lot_id,'source_type':l['source_type'],'source_ref_type':l['source_ref_type'],
   'source_ref_id':l['source_ref_id'],'source_reference_table':table,'source_reference_rows':ref,
   'movements':movements[lot_id],'stocktakes':stocktakes[lot_id],
   'production':completions[lot_id],'preparation_jobs':preparation[lot_id],'assemblies':assemblies[lot_id]}
 def process_tokens(s):return sorted(set(x.strip() for x in re.split(r'[、，,；;\s]+',s or '') if x.strip()))
 basis_fields=set(('product_id','unit','spec','material','flute','assembly','schema'))|set(__import__('app.services.finished_stock_identity',fromlist=['FIELDS']).FIELDS)|{'mold_tool_id','die_cut_path'}
 for r in out['rows']+out['semi_rows']:
  lot_id=r['lot_id'];lot=all_lots[lot_id];chain=lineage(lot_id)
  r['typed_lineage']=chain;r['source_evidence']=[evidence(i) for i in sorted(set(chain['root_ids']+[lot_id]))]
  r['cost']={k:lot[k] for k in ('estimated_unit_cost_snapshot','estimated_square_price_snapshot','estimated_cost_area_m2_snapshot','cost_snapshot_source','cost_snapshot_at')}
  r['cost']['detail']=parsed(lot['cost_snapshot_detail_json'])
  if r['type']!='finished':continue
  d=all_finished.get(lot_id,{});actual=parsed(d.get('physical_basis_json'))
  r['original_physical_basis']=actual
  r['missing_basis_document_fields']=sorted(basis_fields-set(actual or {})) if not actual else []
  from app.services.product_specification import dimension_specification
  partial={'product_id':d.get('product_id'),'box_style':d.get('box_type_snapshot'),
   'spec':dimension_specification(d.get('length_mm'),d.get('width_mm'),d.get('height_mm')),
   'material':d.get('material_code_snapshot'),'flute':d.get('flute_type_snapshot')}
  r['captured_partial_physical_fields']={k:v for k,v in partial.items() if v is not None}
  r['captured_owner_customer_id']=d.get('owner_customer_id')
  r['missing_fields']=sorted(basis_fields-{'schema'}-set(r['captured_partial_physical_fields'])) if not actual else []
  if actual and not actual.get('spec'):r['missing_fields'].append('spec')
  pid=d.get('product_id');root=chain['root_ids'][0] if len(chain['root_ids'])==1 else lot_id
  origin=all_lots[root];origin_detail=all_finished.get(root,{})
  basic_keys=('product_id','owner_customer_id','is_general','inventory_code_snapshot','box_type_snapshot','length_mm','width_mm','height_mm','material_code_snapshot','flute_type_snapshot')
  r['transfer_identity_conflicts']=[k for k in basic_keys if d.get(k)!=origin_detail.get(k)] if root!=lot_id else []
  versions=master[pid];time_versions=[v for v in versions if v['created_at']<=origin['created_at']]
  at_version=time_versions[-1] if time_versions else None
  r['historical_master']={'origin_lot_id':root,'origin_created_at':origin['created_at'],'version_count':len(versions),
   'as_of_version':{k:at_version[k] for k in ('id','version','created_at','snapshot_sha256','action','source')} if at_version else None,
   'as_of_snapshot':at_version['snapshot'] if at_version else None,
   'as_of_basis_fields_absent':[k for k in ('die_cut_path','production_notes','sheet_cutting_settings') if at_version and k not in at_version['snapshot']],
   'latest_as_of_is_physical_confirmation':False}
  if at_version:
   s=at_version['snapshot'];r['historical_master']['detail_conflicts']=[k for k,dk in [('product_code','inventory_code_snapshot'),('box_style','box_type_snapshot'),('length_mm','length_mm'),('width_mm','width_mm'),('height_mm','height_mm')]
    if str(s.get(k))!=str(d.get(dk)) and (k not in ('length_mm','width_mm','height_mm') or str(s.get(k)).removesuffix('.0000')!=str(d.get(dk)))]
  recover=[]
  for node in sorted(set([e['source_lot_id'] for e in chain['edges']])):
   fd=all_finished.get(node,{});basis=parsed(fd.get('physical_basis_json'))
   if isinstance(basis,dict) and basis_fields.issubset(basis) and all(fd.get(k)==d.get(k) for k in basic_keys):
    recover.append({'kind':'typed_source_basis','lot_id':node,'basis':basis,'capture_time_requires_validation':True})
  # A compiled frozen production graph is stronger than a current master.
  for src in r['source_evidence']:
   for production in src['production']:
    order_id=production.get('order_item_id')
    if order_id and pid:
     try:
      from app.services.finished_stock_identity import order_product_basis
      frozen=order_product_basis(db,order_id,pid)
      if frozen:recover.append({'kind':'production_frozen_order_graph','order_item_id':order_id,'basis':parsed(frozen)})
     except Exception as e:r.setdefault('recovery_errors',[]).append(err(e))
  signatures={json.dumps(v['basis'],sort_keys=True,ensure_ascii=False) for v in recover}
  r['recovery_evidence']=recover
  if r.get('identity')=='master_basis_error':group='bad_bom_or_master'
  elif not actual:
   if len(signatures)==1 and not chain['issues'] and not r['transfer_identity_conflicts']:
    group='unique_evidence_recovery_preview';r['recovery_basis']=recover[0]['basis']
   else:group='needs_physical_confirmation'
  elif r['identity']=='mismatch':
   diffs=r.get('differences',{})
   if set(diffs)=={'production_process'} and process_tokens(diffs['production_process']['stock'])==process_tokens(diffs['production_process']['expected']):group='format_only_preview'
   else:group='preserve_historical_difference'
  elif r['identity'] in ('exact','explicit_shared_confirmation','explicit_confirmation','proven_assembly'):group='existing_evidence_compatible'
  else:group='needs_physical_confirmation'
  r['governance_group']=group
  r['required_confirmation_fields']=r['missing_fields'] if group=='needs_physical_confirmation' else []
  if group=='preserve_historical_difference':r['required_confirmation_fields']=sorted(r.get('differences',{}))
  if chain['issues'] or r['transfer_identity_conflicts']:r['required_confirmation_fields']+=['typed_lineage_conflict']
  r['next_action']={'unique_evidence_recovery_preview':'核对来源basis捕获时点与链冲突后，预览登记来源证明，不写当前主档',
   'needs_physical_confirmation':'确认真正缺少的实物身份字段，保留数量/成本/位置，不重复盘点入库',
   'format_only_preview':'预览工艺分隔符/顺序规范化，保留原文，不自动认定新增工艺等价',
   'preserve_historical_difference':'保留旧身份；匹配旧订单冻结身份，新版适用须明确兼容证明',
   'existing_evidence_compatible':'在子副本验证适用订单实际预占/释放/消费，候选可见不代表全链成功',
   'bad_bom_or_master':'维护具体BOM失效节点，库存保持可见，禁止猜造配方'}[group]
 for r in out['semi_rows']:
  p=r.get('profile') or {};missing=[]
  if not p:missing+=['goods_profile.processing','goods_profile.scope','goods_profile.product_ids/customer_ids','face_paper','usage_confirmed']
  if 'processed_usage_unconfirmed' in r['issues']:missing+=['processed_product_or_mold_version','blank_unprinted','remaining_processes']
  for k in ('component_type','pieces_per_box','yield_per_sheet','board_length_mm','board_width_mm'):
   if not r.get(k):missing.append(k)
  if p.get('output_piece') and not r.get('completed_eligible'):missing+=['completed_physical_basis_vs_target']
  r['missing_fields']=missing
  r['governance_group']='semi_needs_confirmation' if missing else 'semi_existing_facts'
  r['next_action']='按原片料/加工事实验证合法用途，不由currentmaster重写材质、数量和成本'
 # Read each known processed binding and exact rectangular raw target through
 # actual candidate and hard-authorization services. This is not a write test.
 from app.services.semi_finished_inventory import (
  SemiFinishedSignature, semi_finished_candidates_for_product,
  ensure_semi_finished_lot_eligibility, theoretical_product_yield)
 from app.services.warehouse_goods import qualification_issues
 from app.services.warehouse_inventory import normalize_material_code
 products=list(db.scalars(select(Product).where(Product.deleted_at.is_(None))))
 for r in out['semi_rows']:
  lot=db.get(InventoryLot,r['lot_id']);detail=lot.semi_finished_detail;profile=r.get('profile') or {}
  target_ids=set(r['allowed_product_ids'])|set(profile.get('product_ids') or [])
  if not profile or profile.get('processing')=='raw':
   target_ids.update(p.id for p in products if (not profile.get('customer_ids') or p.customer_id in profile['customer_ids'])
    and p.report_length_mm==detail.board_length_mm and p.report_width_mm==detail.board_width_mm)
  r['qualification_pairs']=[]
  for pid in sorted(target_ids):
   p=db.get(Product,pid);pair={'product_id':pid,'current_master_target_only':True,'reservation_tested':False}
   try:
    if p is None:raise ValueError('已绑定产品不存在')
    component=detail.component_type
    length=p.base_report_length_mm if component=='base' else p.report_length_mm
    width=p.base_report_width_mm if component=='base' else p.report_width_mm
    material=p.material.code if p.material else p.default_material_code or p.legacy_material_text
    pieces=1 if component in ('cover','base') else int(p.pieces_per_box or 1)
    output=theoretical_product_yield(p,component)
    signature=SemiFinishedSignature(p.customer_id,length,width,normalize_material_code(material),p.flute_type,component,pieces,output)
    pair['product_code']=p.product_code;pair['qualification_issues']=qualification_issues(db,lot,p)
    candidates=semi_finished_candidates_for_product(db,product_id=pid,customer_id=p.customer_id,
     board_length_mm=length,board_width_mm=width,material_code=material,flute_type=p.flute_type,
     component_type=component,pieces_per_box=pieces,stock_yield_per_sheet=output,layer_count=p.layer_count)
    shown=next((c for c in candidates if c.lot.id==lot.id),None)
    pair['candidate_returned']=shown is not None
    pair['candidate_direct_deduction_eligible']=shown.direct_deduction_eligible if shown else False
    for reviewed in (False,True):
     key='reviewed_authorization' if reviewed else 'ordinary_authorization'
     try:pair[key]={'accepted':True,'scope':ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=pid,customer_id=p.customer_id,expected=signature,reviewed=reviewed)}
     except Exception as error:pair[key]={'accepted':False,'error':err(error)}
   except Exception as error:pair['error']=err(error)
   r['qualification_pairs'].append(pair)
  if not target_ids:r['qualification_scope_note']='未有明确产品绑定或当前同尺寸目标；不推断通用原料无合法用途'
 # Existing frozen graph identity is examined independently of current master.
 frozen_targets=collections.defaultdict(list);out['frozen_order_errors']=[]
 from app.services.multilevel_bom_orders import read_compiled_order_bom
 from app.services.finished_stock_identity import compiled_product_bases, order_product_basis
 for graph in rows('order_bom_graphs'):
  oid=graph['order_item_id']
  try:
   compiled=read_compiled_order_bom(db,oid)
   for pid,basis in compiled_product_bases(compiled).items():frozen_targets[pid].append((oid,basis))
  except Exception as error:out['frozen_order_errors'].append({'order_item_id':oid,**err(error)})
 for r in out['rows']:
  basis=all_finished[r['lot_id']]['physical_basis_json']
  r['matching_existing_frozen_order_item_ids']=[oid for oid,wanted in frozen_targets[r.get('product_id')] if matches_stock_identity(basis,wanted)]
 out['historical_restore_limit']={'no_complete_typed_or_frozen_basis_found':not any(r['recovery_evidence'] for r in out['rows'] if r['identity']=='missing_identity'),
  'as_of_master_is_context_not_physical_confirmation':True}
 # Compare the rejected older strict candidate function read-only, without merging it.
 import app.services.warehouse_inventory as warehouse_module
 old_path=pathlib.Path('/Users/kimiyf/ERP-Migration/inventory-closed-loop-20261013/app/services/warehouse_inventory.py')
 node=next(n for n in ast.parse(old_path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='finished_inventory_candidates_for_product')
 node.name='old_strict_candidate';module=ast.Module(body=[node],type_ignores=[])
 namespace=dict(vars(warehouse_module));exec(compile(ast.fix_missing_locations(module),str(old_path),'exec'),namespace)
 strict={};strict_errors={}
 for r in out['rows']:
  pid=r.get('product_id');product=db.get(Product,pid) if pid else None
  if product and pid not in strict:
   try:strict[pid]={lot.id for lot in namespace['old_strict_candidate'](db,product_id=pid,customer_id=product.customer_id)}
   except Exception as e:strict[pid]=set();strict_errors[pid]=err(e)
  r['old_strict_candidate_would_return']=r['lot_id'] in strict.get(pid,set())
 out['strict_candidate_comparison']={'path':str(old_path),'sha256':digest(old_path),'function_only_using_current_shared_helpers':True,'enabled':False,'errors':strict_errors}
 out['counts']=dict(collections.Counter(r['identity'] for r in out['rows']))
 out['groups']=dict(collections.Counter(r['governance_group'] for r in out['rows']+out['semi_rows']))
 out['actual_product_candidate_counts']=dict(collections.Counter(r['identity'] for r in out['rows'] if r.get('current_product_candidate')))
 out['old_strict_candidate_counts']=dict(collections.Counter(r['identity'] for r in out['rows'] if r['old_strict_candidate_would_return']))
 out['typed_transfer_total']=len(transfers)
 out['recovery_previews']=[{'lot_id':r['lot_id'],'version':all_lots[r['lot_id']]['version'],'basis':r.get('recovery_basis'),
  'evidence':r.get('recovery_evidence'),'quantity_cost_location_unchanged':True,'requires_capture_time_validation':True}
  for r in out['rows'] if r['governance_group']=='unique_evidence_recovery_preview']
 out['focus']={str(i):next(r for r in out['rows']+out['semi_rows'] if r['lot_id']==i) for i in (666,680,1430,747)}
 out['focus']['747']['bad_bom_nodes']=rows('products',
  'id IN (SELECT component_product_id FROM product_bom_components WHERE parent_product_id=?)',
  (out['focus']['747']['product_id'],))
 out['focus']['747']['bom_edges']=rows('product_bom_components','parent_product_id=?',(out['focus']['747']['product_id'],))
 out['focus']['000204_procurement_chain']={t:rows(t,w) for t,w in (
  ('material_requisition_items','id=206'),('requisition_item_bom_sources','id=21'),
  ('purchase_purpose_source_snapshots','id=247'),('procurement_source_links','id=31'),
  ('supplier_requisition_order_items','id=623'),('supplier_requisition_orders','id=209'))}
 from decimal import Decimal
 input_cost=Decimal(str(all_lots[680]['estimated_unit_cost_snapshot']))
 finished_cost=Decimal(str(all_lots[1430]['estimated_unit_cost_snapshot']))
 out['focus']['000204_cost_source_preview']={
  'source_lot':680,'finished_lot':1430,'database_has_direct_input_link':False,
  'user_confirmed_input_sheets':100,'user_confirmed_output_units':50,
  'current_raw_sheet_reference_cost':str(input_cost),'current_finished_reference_cost':str(finished_cost),
  'input_reference_total':str(input_cost*100),'output_reference_total':str(finished_cost*50),
  'input_reference_cost_per_output_if_approved':str(input_cost*2),
  'difference_reference_total':str(input_cost*100-finished_cost*50),
  'is_actual_cash_cost':False,'executed':False,
  'basis':'仅现有批次暂估成本及用户确认的100张→50只；不是生产外键或现金结算证据'}
 from app.services.shared_finished_stock import preview as shared_preview, _preview_lots
 focus120={'order_item_id':10552,'confirmed_sets':50,'source_lot_ids':[666,1421],
  'snapshots':rows('sales_order_item_bom_components','sales_order_item_id=10552'),
  'expected_bases':{str(pid):parsed(order_product_basis(db,10552,pid)) for pid in (3496,3787,3863)},
  'historical_basis_overwritten':False,'shared_service_probes':[]}
 for label,call in [('single_member',lambda:shared_preview(db,product_ids=[3496],lot_ids=[666])),
                    ('lot_contract',lambda:_preview_lots(db,[db.get(Product,3496)],[666]))]:
  try:focus120['shared_service_probes'].append({'probe':label,'result':call()})
  except Exception as error:focus120['shared_service_probes'].append({'probe':label,**err(error)})
 out['focus']['000120_order10552_compatibility_gap']=focus120
 current_json=pathlib.Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/current-business-readonly.json')
 captured=json.loads(current_json.read_text());captured_lots={r['id']:r for r in captured['tables']['inventory_lots']}
 out['upstream_evidence_check']={'at':captured['at'],'json_sha256':digest(current_json),
  'all_1430_lot_rows_equal':all(captured_lots[k]==v for k,v in all_lots.items()),
  'database_bytes_equal':captured['database_bytes']==DB.stat().st_size}
 out['source_sha256']={n:digest(SOURCE/n) for n in ('app/services/warehouse_inventory.py','app/services/finished_stock_identity.py','app/services/semi_finished_inventory.py','app/services/warehouse_goods.py')}
 db.rollback()
engine.dispose()
after=digest(DB);assert before==after
out.update(database_sha256_before=before,database_sha256_after=after,database_unchanged=before==after)
assert len(out['rows'])+len(out['semi_rows'])==691
OUTPUT.mkdir(parents=True,exist_ok=True)
(OUTPUT/'all-live-stock-evidence.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str))
lines=['现存库存逐批只读治理；不是正式修复执行记录','分类 '+json.dumps(out['groups'],ensure_ascii=False),'原分类 '+json.dumps(out['counts'],ensure_ascii=False)]
for r in out['rows']+out['semi_rows']:
 lines.append(f"{r['lot_id']} | {r.get('code') or r.get('name')} | {r['governance_group']} | 可用{r['available']} 占用{r['reserved']} {r['unit']} | 来源根{r['typed_lineage']['root_ids']} | 缺字段{','.join(r['missing_fields'])} | {r['next_action']}")
(OUTPUT/'all-live-stock-ledger.txt').write_text('\n'.join(lines)+'\n')
pending=[]
for row in out['rows']:
 if row['governance_group']=='format_only_preview':
  pending.append({'lot_id':row['lot_id'],'action':'format_comparison_preview_only',
   'original_basis':row['original_physical_basis'],'differences':row['differences'],
   'legacy_shared_normalizer_must_validate_sequence':True,'executed':False})
 if row['identity']=='missing_identity' and row['historical_master']['as_of_snapshot']:
  pending.append({'lot_id':row['lot_id'],'action':'partial_history_context_requires_confirmation',
   'captured_fields':row['captured_partial_physical_fields'],
   'historical_version':row['historical_master']['as_of_version'],
   'historical_fields':row['historical_master']['as_of_snapshot'],
   'absent_in_old_snapshot':row['historical_master']['as_of_basis_fields_absent'],
   'missing_fields':row['missing_fields'],'is_complete_restore':False,'executed':False})
(OUTPUT/'pending-evidence-previews.json').write_text(json.dumps({
 'unique_complete_restore_count':len(out['recovery_previews']),
 'format_preview_count':sum(p['action']=='format_comparison_preview_only' for p in pending),
 'partial_history_context_count':sum(p['action']=='partial_history_context_requires_confirmation' for p in pending),
 'previews':pending},ensure_ascii=False,indent=2,default=str))
summary={k:out[k] for k in ('at','groups','counts','actual_product_candidate_counts','old_strict_candidate_counts','typed_transfer_total','database_unchanged','database_sha256_before')}
(OUTPUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False))
