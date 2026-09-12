"""Read-only source backup and isolated additive migration rehearsal."""
import hashlib, json, os, sqlite3, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

source=Path('D:/纸箱厂erp软件搭建/data/carton_erp.sqlite3').resolve()
directory=Path(tempfile.mkdtemp(prefix='stock-preparation-rehearsal-'))
target=directory/'isolated.sqlite3'
assert target.resolve()!=source and not target.exists()
with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dest:
    src.execute('pragma query_only=on');src.backup(dest)

def facts():
    with sqlite3.connect(target) as c:
        result={}
        for table in ('incoming_receipts','incoming_receipt_items','stock_replenishment_orders','stock_replenishment_order_items','inventory_lots','inventory_movements','inventory_reservations','sales_orders','sales_order_items','production_tasks','production_completions'):
            h=hashlib.sha256();n=0
            for row in c.execute(f'SELECT * FROM {table} ORDER BY id'):
                h.update(repr(row).encode());n+=1
            result[table]=dict(count=n,sha256=h.hexdigest())
        assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert not c.execute('pragma foreign_key_check').fetchall()
        return result

before=facts()
env=dict(os.environ,ERP_DATABASE_PATH=str(target),ERP_ENV='development',PYTHONIOENCODING='utf-8')
for action,rev in [('upgrade','sprep0912'),('downgrade','rp0912'),('upgrade','sprep0912')]:
    subprocess.run([sys.executable,'-m','alembic','-x',f'expected_database_path={target}',action,rev],env=env,check=True,capture_output=True)
    assert facts()==before

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.stock_preparation import list_rows
with Session(create_engine('sqlite:///'+target.as_posix())) as db:
    rows=list_rows(db,query='000205')
    found=[dict(receipt_id=r['receipt_item_id'],name=r['name'],quantity=r['quantity'],location=r['location'],status=r['status']) for r in rows]
    assert {386,387}.issubset({r['receipt_id'] for r in found})
    assert all(r['status']=='arrange' for r in found if r['receipt_id'] in (386,387))
report=dict(isolated_database=str(target),original_facts_unchanged=True,integrity='ok',foreign_keys=0,source_rows=found)
with sqlite3.connect(target) as c:
    c.execute("INSERT INTO stock_preparation_commands(operation_key,receipt_item_id,request_json,result_json,actor_id) VALUES('isolated-guard-check',386,'{}','{}',1)")
with sqlite3.connect(target) as c:
    try:
        c.execute("DELETE FROM stock_preparation_commands")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError('immutable command protection absent')
before_block=hashlib.sha256(target.read_bytes()).hexdigest()
blocked=subprocess.run([sys.executable,'-m','alembic','-x',f'expected_database_path={target}','downgrade','rp0912'],env=env,capture_output=True)
assert blocked.returncode != 0
assert hashlib.sha256(target.read_bytes()).hexdigest()==before_block
report['nonempty_downgrade_blocked_unchanged']=True
report['immutable_commands_checked']=True
(directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
