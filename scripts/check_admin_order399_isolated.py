"""Explicit isolated-copy rehearsal; the formal source is opened read-only."""
import os, json, sqlite3, sys
from pathlib import Path
os.environ['ERP_ALLOWED_ORIGINS'] = 'http://localhost:8000'
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from app.api.orders import Order
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from app.services.admin_order_reversal import preview, execute, rows_snapshot

target = Path(sys.argv[1]).resolve()
assert str(target).startswith('D:\\erp-admin-order399-20260914\\data\\audit\\') and target.suffix == '.sqlite3'
target.parent.mkdir(parents=True,exist_ok=True)
if not target.exists():
    with sqlite3.connect('file:D:/TianmingERP/shared/data/carton_erp.sqlite3?mode=ro',uri=True) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
engine = create_engine('sqlite:///' + target.as_posix())
@event.listens_for(engine, 'connect')
def foreign_keys(conn, record):
    conn.execute('PRAGMA foreign_keys=ON')
with Session(engine) as db:
    actor = db.scalar(select(User).where(User.role=='admin',User.is_active==True).order_by(User.id))
    assert actor
    before = rows_snapshot([db.get(InventoryLot,i) for i in (682,683)])
    plan = preview(db,user=actor,order_ids=[9737],mode='delete_trial')
    print(json.dumps(plan, ensure_ascii=False, default=str,indent=2),flush=True)
    result = execute(db,user=actor,order_ids=[9737],mode='delete_trial',reviewed_hash=plan['reviewed_hash'],
        operation_key='isolated-admin399-trial',trial_confirmed=True,reason='隔离演练纯模拟订单清理')
    db.commit()
    assert rows_snapshot([db.get(InventoryLot,i) for i in (682,683)]) == before
    assert db.get(Order,9737).status=='cancelled'
    assert db.execute(__import__('sqlalchemy').text('PRAGMA foreign_key_check')).first() is None
    for lid in range(688,694):
        lot=db.get(InventoryLot,lid)
        assert lot.quantity_available + lot.quantity_reserved == 0, (lid,lot.quantity_available,lot.quantity_reserved)
    print('PASS: isolated only; real stock 682/683 unchanged; trial outputs closed; FK clean.',result)
