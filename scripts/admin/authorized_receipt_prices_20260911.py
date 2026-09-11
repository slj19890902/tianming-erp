"""Apply only the 20 missing historical receipt prices authorized 2026-09-11."""
import argparse,json,sqlite3,sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import Session
from app.models.user import User
from app.models.audit import OperationLog
from app.services.supplier_receipt_price_facts import adopt_historical_price_facts,HISTORICAL_ADOPTION_REASON

TASK='owner-authorized-receipt-current-prices-20260911'
def main():
 p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--plan',required=True);p.add_argument('--backup-reference',required=True);p.add_argument('--output',required=True);args=p.parse_args()
 planned=json.loads(Path(args.plan).read_text(encoding='utf-8'));assert sorted(x['incoming_receipt_item_id'] for x in planned['eligible'])==list(range(1,21))
 with sqlite3.connect(f"file:{Path(args.backup_reference).as_posix()}?mode=ro",uri=True) as backup:
  assert backup.execute('pragma integrity_check').fetchall()==[('ok',)] and not backup.execute('pragma foreign_key_check').fetchall()
 conn=sqlite3.connect(f"file:{Path(args.database).as_posix()}?mode=rw",uri=True);conn.execute('pragma foreign_keys=on');engine=create_engine('sqlite://',creator=lambda:conn)
 with Session(engine) as db:
  db.execute(text('BEGIN IMMEDIATE'))
  assert db.execute(text('select version_num from alembic_version')).scalars().all()==['rw10v8x9z71']
  if db.scalar(select(OperationLog.id).where(OperationLog.batch_id==TASK)):
   db.rollback();print('already applied');return
  user=db.get(User,1);assert user and user.role=='admin' and user.is_active
  before=conn.total_changes
  result=adopt_historical_price_facts(db,settlement_month='all-authorized',start_utc=datetime(2000,1,1),end_utc=datetime(2100,1,1),selections=[(x['incoming_receipt_item_id'],x['source_hash']) for x in planned['eligible']],confirmation_text=HISTORICAL_ADOPTION_REASON,plan_hash=planned['plan_hash'],backup_reference=args.backup_reference,user=user)
  assert result['created_count']==20
  result.update(task=TASK,authorization='老板2026-09-11确认缺冻结价实收采用当前纸板价格',basis='按稳定订单ID读取原有且一致的报料快照；未改供应商报料/订单尺寸；当前价采用单独记录，不冒充收料当日价',plan=planned)
  db.add(OperationLog(user_id=None,username='Codex owner-authorized maintenance',action='cost_backfill',resource='SupplierReceiptSettlementPriceFact',description=result['authorization'],details=json.dumps(result,ensure_ascii=False,default=str),event_category='data_change',result='success',source='admin_script',module_code='finance',action_code='receipt.current_price_adoption',operator_name_snapshot='Codex（老板本次授权）',object_ref=TASK,batch_id=TASK,schema_version=1))
  db.flush();assert conn.total_changes-before==21;assert not db.execute(text('pragma foreign_key_check')).all();db.commit()
 Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8');print('created 20 receipt price facts and 1 audit')
if __name__=='__main__':main()
