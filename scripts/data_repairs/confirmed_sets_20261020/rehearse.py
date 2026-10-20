"""Foreground entry used only by the standard owned UAT task runner."""
import json,os,sys,importlib.util
from pathlib import Path
S=Path(__file__).parent
cfg=json.loads((S/'transaction.json').read_text())
T=Path('/Library/TianmingERP/published')/cfg['target']/'migration'
sys.path.insert(0,str(T));sys.path.insert(0,str(S))
import app.models
from app.core.database import engine
from sqlalchemy.orm import Session
from app.models.user import User
import operation,guard
plan=json.loads((S/'plan.json').read_text())
assert os.environ['ERP_ENVIRONMENT']=='test' and os.environ.get('ERP_UAT_ROOT')
assert Path(os.environ['ERP_DATABASE_PATH']).resolve().is_relative_to(Path(os.environ['ERP_UAT_ROOT']).resolve())
# First force a failure after BOTH identity corrections. No partial commit may survive.
with engine.connect() as c:
 c.exec_driver_sql('BEGIN IMMEDIATE');before=guard.capture(c)
 with Session(bind=c,join_transaction_mode='create_savepoint') as db:
  real=operation.reserve_finished_inventory_for_bom_component
  def fail(*args,**kwargs):raise RuntimeError('injected reservation failure')
  operation.reserve_finished_inventory_for_bom_component=fail
  try:
   try:operation.execute(db,db.get(User,1),plan)
   except RuntimeError as e:assert str(e)=='injected reservation failure'
   else:raise AssertionError('failure injection not reached')
  finally:operation.reserve_finished_inventory_for_bom_component=real
  db.rollback()
 c.rollback()
 assert guard.capture(c)==before, 'partial mutation after rollback'
 c.rollback()
# Reject changed scope, then execute and prove full-row write boundary and replay.
with engine.connect() as c:
 c.exec_driver_sql('BEGIN IMMEDIATE');before=guard.capture(c)
 with Session(bind=c,join_transaction_mode='create_savepoint') as db:
  changed=json.loads(json.dumps(plan));changed['lots'][0]['lot']['quantity_available']-=1
  try:operation.execute(db,db.get(User,1),changed)
  except AssertionError:pass
  else:raise AssertionError('stale plan accepted')
  result=operation.execute(db,db.get(User,1),plan)
  after=guard.capture(c);scope=guard.verify(before,after)
  assert operation.execute(db,db.get(User,1),plan)==result
  assert guard.capture(c)==after, 'replay caused extra writes'
  db.commit()
 c.commit()
with engine.connect() as c:
 assert not c.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
 assert c.exec_driver_sql('PRAGMA integrity_check').scalar()=='ok'
 with Session(bind=c) as db:assert operation.verify(db,plan)==result
print(json.dumps(dict(status='passed',rollback_atomic=True,stale_plan_rejected=True,idempotent=True,scope=scope,result=result),ensure_ascii=False),flush=True)
