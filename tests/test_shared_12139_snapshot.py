"""One explicit snapshot, in-memory transaction only; no attachment access."""
import json,os,sqlite3
from pathlib import Path
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from app.models import *
from app.services import shared_customer_access as access
from scripts.admin.confirm_shared_12139_access import plan,apply


def test_reviewed_12139_snapshot_in_memory():
    name=os.environ.get('ERP_12139_READONLY_SNAPSHOT')
    if not name:pytest.skip('explicit read-only snapshot required')
    source=sqlite3.connect(Path(name).as_uri()+'?mode=ro',uri=True)
    source.execute('PRAGMA query_only=ON')
    memory=sqlite3.connect(':memory:',check_same_thread=False);source.backup(memory);source.close()
    engine=create_engine('sqlite://',creator=lambda:memory)
    with Session(engine) as db:
        db.execute(text('PRAGMA foreign_keys=ON'))
        value,_=plan(db)
        result=apply(db,expected_hash=value['preview_hash'],backup_receipt={'verified':True,'sha256':'a'*64})
        db.commit()
        assert apply(db,expected_hash=value['preview_hash'],backup_receipt={'verified':True,'sha256':'a'*64})['replayed']
        assert not db.execute(text('PRAGMA foreign_key_check')).fetchall()
        out=Path('/Users/kimiyf/ERP-Migration/evidence/shared-stock-12139-20261017')
        (out/'reviewed-data-plan.json').write_text(access.canonical(value))
        (out/'isolated-data-validation.json').write_text(access.canonical(result))
