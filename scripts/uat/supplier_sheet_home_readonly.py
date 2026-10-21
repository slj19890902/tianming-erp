"""Read-only reproduction against the owned database provided by run_task.py."""
import json
import os
import sys
import time
import cProfile
import sqlite3
import hashlib
from pathlib import Path
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
import app.models
from app.models.user import User
from app.api.dashboard import dashboard_overview
from app.api.warehouse import _order_manual_sheet_state

evidence = Path(sys.argv[1])
database = os.environ['ERP_DATABASE_PATH']
# Schema rehearsal happens only in the runner-owned copy, before query_only.
def facts():
    with sqlite3.connect(database) as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")]
        result = {}
        for table in tables:
            if table in ('users','sessions','email_intake_settings'):
                continue
            cols = [r[1] for r in c.execute('PRAGMA table_info("'+table+'")')
                    if r[1] not in ('supplier_edge_type','base_supplier_edge_type')
                    and not any(s in r[1].lower() for s in ('password','secret','token','credential'))]
            quoted = ','.join('"'+s+'"' for s in cols)
            digest = hashlib.sha256()
            for row in c.execute('SELECT '+quoted+' FROM "'+table+'"'):
                digest.update(json.dumps(row,default=str).encode());digest.update(b'\n')
            result[table] = digest.hexdigest()
        schema = c.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL AND name NOT LIKE '%supplier_edge_immutable' ORDER BY type,name").fetchall()
        return result,schema
before_facts = facts()
from alembic.config import Config
from alembic import command
cfg = Config('alembic.ini')
command.upgrade(cfg,'ey1021se')
assert facts()==before_facts
command.downgrade(cfg,'ex1021cp')
assert facts()==before_facts
command.upgrade(cfg,'ey1021se')
assert facts()==before_facts
with sqlite3.connect(database) as c:
    assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
engine = create_engine('sqlite+pysqlite:///file:'+os.environ['ERP_DATABASE_PATH']+'?mode=ro&uri=true')
@event.listens_for(engine, 'connect')
def readonly(connection, _):
    connection.execute('PRAGMA query_only=ON')
queries = Counter()
@event.listens_for(engine, 'before_cursor_execute')
def count(_c, _cu, statement, _params, _ctx, _many):
    queries[' '.join(statement.split())] += 1
user = User(id=1, username='isolated-audit', role='admin', is_active=True, customer_access_mode='all')
with Session(engine, autoflush=False) as db:
    profile = cProfile.Profile()
    start = time.monotonic()
    profile.enable()
    data = dashboard_overview(db=db, user=user)
    profile.disable()
    elapsed = time.monotonic()-start
before = json.loads((evidence/'dashboard-view-data.json').read_text())
after = {key:data.get(key) for key in ('workbench', 'low_stock_warnings')}
normalized = json.loads(json.dumps(after, default=str))
assert normalized == before, 'dashboard business projections changed'
query_count = sum(queries.values())
report = {'seconds':elapsed, 'query_count':query_count, 'projection_equal':True,
    'baseline_query_count':8390, 'source':'owned copy; query_only', 'production_writes':False,
    'migration_roundtrip':'ex1021cp -> ey1021se -> ex1021cp -> ey1021se',
    'old_table_facts_preserved':len(before_facts[0]),'old_indexes_triggers_preserved':len(before_facts[1])}
report['matching'] = {}
with Session(engine, autoflush=False) as db:
    for item_id in (11041, 11042, 11032):
        rows = _order_manual_sheet_state(db,item_id,'whole',user,'auto')[3]
        report['matching'][str(item_id)] = rows
    c14 = {row['lot_id']:row for row in report['matching']['11042']['items']}
    c36 = {row['lot_id']:row for row in report['matching']['11032']['items']}
    assert c14[1312]['stock_yield_per_sheet'] == 30
    assert c14[1312]['max_stock_quantity'] == 10
    assert c36[1315]['stock_yield_per_sheet'] == 8
    assert c36[1315]['max_stock_quantity'] == 4
(evidence/'implementation-readonly-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
print(json.dumps({k:v for k,v in report.items() if k!='matching'},ensure_ascii=False))
