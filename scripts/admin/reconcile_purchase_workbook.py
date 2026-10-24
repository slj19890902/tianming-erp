"""Reviewed maintenance operation; native caller owns backup, lock and commit.

The manifest is private evidence, never a web payload. No procurement, receipt,
order, reservation, inventory, price or finance rows can be written here.
"""
from __future__ import annotations
import hashlib,json,re
from decimal import Decimal,InvalidOperation
from sqlalchemy import select
from app.models.product import Product
from app.models.material import Material
from app.models.audit import OperationLog
from app.models.shared_finished_stock import SharedFinishedMember
from app.services.master_data_versioning import apply_versioned_update,normalize_json_value
from app.services.audit_log import append_audit_event
from app.services.supplier_master import resolve_supplier,SupplierLookupError
from app.services.purchase_workbook_history import normalize_trace,record_trace,trace_key,ACTION
from app.core.time_contract import beijing_now_naive

FIELDS={'material_id','default_material_code','default_cardboard_length','default_cardboard_width','supplier_edge_type','crease_type','crease_left_mm','crease_middle_mm','crease_right_mm','default_score_lines'}
SUMMARY_ACTION='reconcile_purchase_workbook_batch'

def canonical(value):return json.dumps(normalize_json_value(value),ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def same(a,b):
 if a is None or b is None:return a is b
 try:return Decimal(str(a))==Decimal(str(b))
 except (InvalidOperation,ValueError):return a==b

def product_map(db,identities):
 ids=[int(i) for i in identities];result={}
 for offset in range(0,len(ids),500):
  result.update({p.id:p for p in db.scalars(select(Product).where(Product.id.in_(ids[offset:offset+500])))})
 return result

def prepare(db,manifest):
 if not re.fullmatch('[a-f0-9]{64}',manifest.get('source_sha256','')):raise ValueError('来源摘要无效')
 if len(manifest.get('traces',[]))>20000 or len(manifest.get('master_repairs',[]))>1000:raise ValueError('固定维护范围过大')
 identities=manifest['identities'];products=product_map(db,identities)
 invalid={};deferred=[];changes=[];traces=[];already=0
 for sid,v in identities.items():
  p=products.get(int(sid))
  if not p or not p.is_active or p.deleted_at is not None or p.purged_at is not None or p.customer_id!=v['customer_id'] or p.product_code!=v['product_code'] or p.customer_material_code!=v['customer_material_code']:
   invalid[int(sid)]='产品或客户身份已变化'
 for c in manifest['master_repairs']:
  pid=c['product_id'];p=products.get(pid);reason=invalid.get(pid)
  if not reason and (p.version!=c['expected_version'] or any(not same(getattr(p,k),v) for k,v in c['before'].items())):reason='主档版本或修改前值已变化'
  if not reason and (p.is_composite or p.is_virtual_composite_parent or p.supply_mode!='corrugated_production' or db.get(SharedFinishedMember,pid)):reason='生产或共用结构需要单独核对'
  if not reason and (p.material_id!=c['material_id_unchanged'] or not set(c['after'])<=FIELDS):reason='超出固定默认字段范围'
  if not reason:
   material=db.get(Material,c.get('target_material_id',p.material_id))
   if not material or not material.is_active or material.layer_count!=p.layer_count:reason='当前材质或层数需要核对'
   elif 'material_id' in c['after'] and (
    c['after']['material_id']!=c.get('target_material_id')
    or material.code!=c.get('target_material_code') or p.flute_type!=c.get('target_flute')
    or c['after'].get('default_material_code')!=material.code+'/'+p.flute_type
   ):reason='新材质与原表或现行层楞不一致'
   else:
    try:resolve_supplier(db,material.supplier_name,require_active=True)
    except SupplierLookupError:reason='当前供应商已停用或身份不明'
  if not reason:
   after=c['after']
   if 'default_material_code' in after and after['default_material_code']!=material.code+'/'+p.flute_type:reason='默认材质文字与实际材质不一致'
   for legacy,current in [('default_cardboard_length','report_length_mm'),('default_cardboard_width','report_width_mm')]:
    expected=getattr(p,current)
    if legacy in after and (expected is None or not same(after[legacy],Decimal(str(expected))/10)):reason='默认厘米尺寸与毫米尺寸不一致'
   if {'crease_type','crease_left_mm','crease_middle_mm','crease_right_mm','default_score_lines'} & set(after):
    parts=[after.get(k,getattr(p,k)) for k in ['crease_left_mm','crease_middle_mm','crease_right_mm']]
    if after.get('crease_type',p.crease_type)!='压线' or any(v is None or v<=0 for v in parts) or not same(sum(parts),p.report_width_mm):reason='压线尺寸未闭合报料宽度'
   if not after:reason='没有需要修改的字段'
  if reason:deferred.append({'kind':'master','product_id':pid,'source_rows':c['source_rows'],'reason':reason})
  else:changes.append(c)
 normalized=[normalize_trace(v) for v in manifest['traces']]
 keys=[trace_key(v) for v in normalized]
 if len(set(keys))!=len(keys):raise ValueError('固定清单包含重复来源行')
 existing={}
 for offset in range(0,len(keys),500):
  for row in db.scalars(select(OperationLog).where(OperationLog.action_code==ACTION,OperationLog.object_ref.in_(keys[offset:offset+500]))):existing[row.object_ref]=row
 for v,key in zip(normalized,keys):
  if v['source_sha256']!=manifest['source_sha256']:raise ValueError('来源摘要不一致')
  if any(pid in invalid or pid not in products or products[pid].customer_id!=v['customer_id'] for pid in v['related_product_ids']):
   deferred.append({'kind':'trace','product_id':v['product_id'],'source_row':v['row'],'reason':'本行关联的产品/客户身份已变化'});continue
  p=products[v['product_id']];identity=identities[str(p.id)]
  if v['identity_basis']=='explicit_customer_and_exact_finished_dimensions' and any(not same(getattr(p,k),identity[k]) for k in ['length_mm','width_mm','height_mm']):
   deferred.append({'kind':'trace','product_id':p.id,'source_row':v['row'],'reason':'用于匹配的成品尺寸已变化'});continue
  if v['identity_basis']=='explicit_customer_and_exact_named_dimensions' and p.product_name!=identity['product_name']:
   deferred.append({'kind':'trace','product_id':p.id,'source_row':v['row'],'reason':'用于匹配的产品名称已变化'});continue
  if v['identity_basis'] in {'unique_model_label_and_reporting_geometry','code_role_and_reporting_geometry','same_customer_code_and_unique_reporting_geometry'} and (
   p.product_name!=identity['product_name'] or any(not same(getattr(p,k),identity[k]) for k in ['report_length_mm','report_width_mm','base_report_length_mm','base_report_width_mm'])
  ):
   deferred.append({'kind':'trace','product_id':p.id,'source_row':v['row'],'reason':'用于匹配的型号或报料尺寸已变化'});continue
  if v['identity_basis']=='exact_code_and_explicit_component_role' and (
   p.product_name!=identity['product_name'] or p.is_internal_component!=identity['is_internal_component']
  ):
   deferred.append({'kind':'trace','product_id':p.id,'source_row':v['row'],'reason':'用于匹配的BOM子件身份已变化'});continue
  if key in existing:
   semantic={k:x for k,x in v.items() if k not in {'source_sha256','source_name'}}
   if json.loads(existing[key].details or '{}').get('trace_hash')!=digest(semantic):deferred.append({'kind':'trace','product_id':p.id,'source_row':v['row'],'reason':'原表同一行内容已有另一版本'})
   else:already+=1
  else:traces.append(v)
 value={'manifest_hash':digest(manifest),'source_sha256':manifest['source_sha256'],'master_repairs':changes,'traces':traces,'already_imported':already,'deferred':deferred}
 value['review_hash']=digest(value);return value

def apply_reviewed(db,manifest,*,actor,expected_review_hash,backup_receipt,batch_id,inject_after=None):
 if not actor or not actor.is_active or actor.role!='admin':raise ValueError('仅管理员可执行固定维护')
 if not backup_receipt.get('verified') or not re.fullmatch('[a-f0-9]{64}',str(backup_receipt.get('sha256',''))):raise ValueError('缺少已验证恢复点')
 previous=db.scalar(select(OperationLog).where(OperationLog.batch_id==batch_id,OperationLog.action_code==SUMMARY_ACTION))
 if previous:
  value=json.loads(previous.details or '{}')
  if value.get('manifest_hash')!=digest(manifest):raise ValueError('同一维护编号已绑定其他清单')
  return {**value['result'],'replayed':True}
 review=prepare(db,manifest)
 if review['review_hash']!=expected_review_hash:raise ValueError('正式资料与已验证清单变化，停止维护')
 # Retain the exact ORM identities through the batch, avoiding repeated loads.
 product_cache=product_map(db,manifest['identities'])
 for c in review['master_repairs']:
  p=product_cache[c['product_id']]
  apply_versioned_update(db,object_type='product',entity=p,updates=c['after'],expected_version=c['expected_version'],user=actor,reason='采购台账报料默认资料一致性修正；原表行'+','.join(map(str,c['source_rows'])),source='script.purchase_workbook',action='system_consistency_fix')
  p.manual_modified=True
  p.manual_modified_at=beijing_now_naive()
  db.flush()
 imported=0
 for v in review['traces']:
  imported+=int(record_trace(db,v,actor=actor,batch_id=batch_id)['created'])
  if inject_after==imported:raise RuntimeError('Injected batch rollback check')
 result={'master_products_changed':len(review['master_repairs']),'trace_links_imported':imported,'trace_links_reused':review['already_imported'],'deferred_count':len(review['deferred']),'review_hash':review['review_hash'],'procurement_order_inventory_finance_written':False,'replayed':False}
 append_audit_event(db,event_category='business',result='success',source='script',module_code='requisition',action_code=SUMMARY_ACTION,resource='purchase_workbook_trace',actor=actor,batch_id=batch_id,description='已核定采购台账来源及报料默认资料维护',details={'manifest_hash':digest(manifest),'review_hash':review['review_hash'],'backup_sha256':backup_receipt['sha256'],'result':result})
 return result
