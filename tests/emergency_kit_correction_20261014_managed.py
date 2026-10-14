"""True-data, owned-child exercise of the narrowly reviewed 000120 service."""
from pathlib import Path
import hashlib,json,os,sqlite3,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
SOURCE=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/managed-tasks/task-2a4eba8403ba40fba6eff95f2961e553/copies/carton_erp.sqlite3')
CHILD=Path(os.environ['ERP_DATABASE_PATH']).resolve();owned=Path(os.environ['ERP_UAT_ROOT']).resolve()
assert CHILD!=SOURCE and CHILD==owned/'carton_erp.sqlite3' and os.environ.get('ERP_UAT_ISOLATION_ID')
assert json.loads(owned.parent.joinpath('result.json').read_text())['source']==str(SOURCE)
OUTPUT=Path('/Users/kimiyf/ERP-Migration/evidence/erp-plan-v3-20261014/stock-governance-sol')
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
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
result={'baseline':str(SOURCE),'child':str(CHILD),'production_accessed':False,'committed':False,'delivery_modified_by_service':False}
failed=None
with Session(engine,autoflush=False,expire_on_commit=False) as db:
 db.connection().exec_driver_sql('BEGIN')
 try:
  actor=db.scalar(select(User).where(User.role=='admin',User.is_active.is_(True)).order_by(User.id))
  original_allocation=db.get(UnorderedFinishedDeliveryAllocation,87)
  allocation_snapshot={c.name:getattr(original_allocation,c.name) for c in original_allocation.__table__.columns}
  pre=service.preview(db,actor=actor);result['before_outer_retirement_preview']=pre
  # Simulate only the root caller's explicitly required retirement. Preserve
  # the original planned allocation, quantity, and consumed/restored fields.
  target=db.get(DeliveryItem,1027);assert target.is_current and target.delivery_id==154
  target.is_current=False;db.flush()
  proposal=service.preview(db,actor=actor);result['ready_preview']=proposal
  kwargs=dict(actor=actor,preview_hash=proposal['preview_hash'],operation_key='managed-000120-fixed-50',
   evidence='用户确认000120实际50套取自现有上下盖库存；仅此订单各50',physical_match_confirmed=True)
  receipt=service.execute(db,**kwargs);result['receipt']=receipt
  replay=service.execute(db,**kwargs);assert replay['replayed'];result['idempotent_replay']=replay
  assert {c.name:getattr(original_allocation,c.name) for c in original_allocation.__table__.columns}==allocation_snapshot
  result['original_planned_allocation_unchanged']=True
  result['post_lots']=[service._source_document(db.get(InventoryLot,lid)) for lid in (666,1421,receipt['split_lot_id'],receipt['output_lot_id'])]
 except Exception as error:
  import traceback
  failed=error;result['error']={'type':type(error).__name__,'reason':str(error),'frames':traceback.format_exc()[-6000:]}
 finally:db.rollback()
raw.close();raw=connect();after=logical_hash();raw.close();engine.dispose()
assert before==after
assert source_before==file_hash(SOURCE)
result.update(logical_fingerprint_before=before,logical_fingerprint_after=after,baseline_sha256=source_before,baseline_unchanged=True,all_changes_rolled_back=True)
(OUTPUT/'emergency-kit-managed.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
print(json.dumps({k:v for k,v in result.items() if k not in ('ready_preview','before_outer_retirement_preview','post_lots','error')},ensure_ascii=False))
if failed:
 print(str(failed),file=sys.stderr);raise SystemExit(1)
