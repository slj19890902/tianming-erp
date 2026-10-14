"""True-data, owned-child exercise of the narrowly reviewed 000120 service."""
from pathlib import Path
import hashlib,json,os,sqlite3,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
SOURCE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/managed-tasks/task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3')
CHILD=Path(os.environ['ERP_DATABASE_PATH']).resolve();owned=Path(os.environ['ERP_UAT_ROOT']).resolve()
assert CHILD!=SOURCE and CHILD==owned/'carton_erp.sqlite3' and os.environ.get('ERP_UAT_ISOLATION_ID')
assert json.loads(owned.parent.joinpath('result.json').read_text())['source']==str(SOURCE)
OUTPUT=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol')
MAP_SOURCE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/map-preflight-20261014-034856/runtime.json')
MAP_TARGET=Path(os.environ['ERP_TWIN_LAYOUT_RUNTIME_PATH']).resolve()
assert MAP_TARGET.is_relative_to(owned) and MAP_TARGET!=MAP_SOURCE
MAP_TARGET.write_bytes(MAP_SOURCE.read_bytes())
assert MAP_TARGET.read_bytes()==MAP_SOURCE.read_bytes()
from sqlalchemy import create_engine,select
from app.core.receipt_price_guard import ReceiptPriceGuardSession as Session
from app.models import Base
from app.models.user import User
from app.models.delivery import DeliveryItem
from app.models.warehouse_inventory import InventoryLot,UnorderedFinishedDeliveryAllocation
from app.services import emergency_kit_correction_20261014 as service

def file_hash(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
source_before=file_hash(SOURCE)
def connect():
 c=sqlite3.connect(str(CHILD));c.execute('pragma foreign_keys=on');return c
raw=connect()
def logical_hash():
 h=hashlib.sha256()
 for (table,) in raw.execute("select name from sqlite_master where type='table' order by name"):
  h.update(table.encode());width=len(raw.execute('pragma table_info("'+table+'")').fetchall())
  for r in raw.execute('select * from "'+table+'" order by '+','.join(str(n+1) for n in range(width))):
   h.update(json.dumps(r,ensure_ascii=False,default=str,separators=(',',':')).encode());h.update(b'\n')
 return h.hexdigest()
before=logical_hash();engine=create_engine('sqlite://',creator=connect)
result={'baseline':str(SOURCE),'child':str(CHILD),'production_accessed':False,'committed':False,'delivery_modified_by_service':False,'runtime_map_source':str(MAP_SOURCE),'runtime_map_sha256':file_hash(MAP_SOURCE)}
failed=None
result['cases']=[]
for stage in ('success','late_audit_fault'):
 case={'stage':stage,'committed':False}
 with Session(engine,autoflush=False,expire_on_commit=False) as db:
  db.connection().exec_driver_sql('BEGIN')
  original_audit=service.append_audit_event
  try:
   actor=db.scalar(select(User).where(User.role=='admin',User.is_active.is_(True)).order_by(User.id))
   original_allocation=db.get(UnorderedFinishedDeliveryAllocation,87)
   allocation_snapshot={c.name:getattr(original_allocation,c.name) for c in original_allocation.__table__.columns}
   pre=service.preview(db,actor=actor);case['before_outer_retirement_preview']=pre
   target=db.get(DeliveryItem,1027);assert target.is_current and target.delivery_id==154
   target.is_current=False;db.flush()
   proposal=service.preview(db,actor=actor);case['ready_preview']=proposal
   kwargs=dict(actor=actor,preview_hash=proposal['preview_hash'],operation_key='managed-physical-000120-fixed-50',
    evidence='用户确认000120实际50套取自现有上下盖各50；旧订单保持取消；只更正无订单实物组套',physical_match_confirmed=True)
   if stage=='late_audit_fault':
    def fault(*args,**kwargs):
     original_audit(*args,**kwargs)
     raise RuntimeError('managed physical-kit late audit injected')
    service.append_audit_event=fault
   receipt=service.execute(db,**kwargs);case['receipt']=receipt
   assert stage=='success'
   replay=service.execute(db,**kwargs);assert replay['replayed'];case['idempotent_replay']=replay
   from app.models.order import Order
   from app.models.finance import FinanceIdempotencyRecord
   from app.services.inventory_valuation import frozen_cost
   assert db.get(Order,9782).status=='cancelled'
   assert {c.name:getattr(original_allocation,c.name) for c in original_allocation.__table__.columns}==allocation_snapshot
   record=db.get(FinanceIdempotencyRecord,receipt['source_record_id'])
   proof=json.loads(record.response_json)
   assert len(proof['contract']['inputs'])==2 and proof['contract']['total_cost']=='641.3350'
   assert frozen_cost(db.get(InventoryLot,receipt['output_lot_id']),db)[0]==service.UNIT_COST
   from app.core.inventory_entry_guard import validate_entries
   assert receipt['output_lot_id'] in db.info['new_inventory_entry_ids']
   validate_entries(db);case['transaction_entry_guard_passed']=True
   case['finance_receipt']=proof
   case['original_planned_allocation_unchanged']=True
   case['cancelled_order_preserved']=True
   case['post_lots']=[service._source_document(db.get(InventoryLot,lid)) for lid in (666,1421,receipt['split_lot_id'],receipt['output_lot_id'])]
   case['passed']=True
  except Exception as error:
   import traceback
   if stage=='late_audit_fault' and isinstance(error,RuntimeError) and str(error)=='managed physical-kit late audit injected':
    db.expire_all()
    from app.models.finance import FinanceIdempotencyRecord
    assert db.get(InventoryLot,666).quantity_available==210
    assert db.get(InventoryLot,666).finished_detail.physical_basis_json is None
    assert db.get(InventoryLot,1421).quantity_available==210
    assert not list(db.scalars(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.action==service.SOURCE)))
    case['passed']=True;case['fault_reached_after_full_receipt']=True
   else:
    failed=error;case['passed']=False
    case['error']={'type':type(error).__name__,'reason':str(error),'frames':traceback.format_exc()[-6000:]}
  finally:
   service.append_audit_event=original_audit
   db.rollback()
 raw.close();raw=connect();case['fingerprint_after_rollback']=logical_hash()
 assert case['fingerprint_after_rollback']==before
 result['cases'].append(case)
 if failed:break
raw.close();raw=connect();after=logical_hash();raw.close();engine.dispose()
assert before==after
assert source_before==file_hash(SOURCE)
result.update(logical_fingerprint_before=before,logical_fingerprint_after=after,baseline_sha256=source_before,baseline_unchanged=True,all_changes_rolled_back=True)
(OUTPUT/'physical-kit-managed.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
print(json.dumps({**{k:v for k,v in result.items() if k!='cases'},'cases':[{'stage':c['stage'],'passed':c['passed'],'error':c.get('error')} for c in result['cases']]},ensure_ascii=False))
if failed:
 print(str(failed),file=sys.stderr);raise SystemExit(1)
