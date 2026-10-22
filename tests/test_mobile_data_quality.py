import importlib.util
from pathlib import Path
import sqlite3
import pytest

spec = importlib.util.spec_from_file_location('mobile_data_quality', Path(__file__).parents[1]/'scripts/audit/mobile_data_quality.py')
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def fixture(tmp_path):
    database = tmp_path/'snapshot.sqlite3'
    with sqlite3.connect(database) as db:
        db.executescript('''
CREATE TABLE alembic_version(version_num TEXT); INSERT INTO alembic_version VALUES('synthetic');
CREATE TABLE customers(id INTEGER, status TEXT);
CREATE TABLE products(id INTEGER,customer_id INTEGER,product_code TEXT,product_name TEXT,is_active INTEGER,deleted_at TEXT,unit TEXT);
CREATE TABLE inventory_lots(id INTEGER,quantity_available INTEGER,quantity_reserved INTEGER,quantity_consumed INTEGER,quantity_damaged INTEGER,quantity_scrapped INTEGER);
CREATE TABLE inventory_reservations(inventory_lot_id INTEGER,reserved_stock_quantity INTEGER,consumed_stock_quantity INTEGER,released_stock_quantity INTEGER,status TEXT);
CREATE TABLE material_requisitions(id INTEGER,created_at TEXT,status TEXT);
INSERT INTO customers VALUES(1,'active'),(2,'inactive');
INSERT INTO products VALUES(1,1,'A','long',1,NULL,'只'),(2,1,' a ','short',1,NULL,'只'),(3,2,'A','other customer',1,NULL,''),(4,1,'OLD','archived',0,NULL,'');
INSERT INTO inventory_lots VALUES(1,20,5,0,0,0),(2,-1,0,0,0,0);
INSERT INTO inventory_reservations VALUES(1,7,2,0,'partial');
INSERT INTO material_requisitions VALUES(1,'2026-08-01','draft'),(2,'2026-08-01','completed'),(3,'2026-10-20','draft');
''')
    return database


def test_readonly_queue_distinguishes_candidates_from_invalid_facts(tmp_path):
    database=fixture(tmp_path); before=database.read_bytes()
    report=quality.scan(database,captured_at='2026-10-21T21:00:00+08:00',as_of='2026-10-22T18:00:00+08:00')
    assert database.read_bytes()==before
    assert report['complete'] and report['database_unchanged']
    assert report['counts']=={'ambiguous_product_code':2,'inactive_customer_product':1,'missing_product_unit':1,'negative_inventory_bucket':1,'stale_requisition_draft':1}
    assert report['snapshot_age_hours']==21
    assert not report['repairs_executed']
    assert all(not row['repair_preview']['executable'] for row in report['issues'])
    assert not any(row['rule']=='reservation_balance' for row in report['issues'])
    assert len({r['issue_id'] for r in report['issues']})==len(report['issues'])


def test_missing_schema_is_unavailable_not_a_clean_result(tmp_path):
    database=fixture(tmp_path)
    with sqlite3.connect(database) as db:db.execute('DROP TABLE inventory_reservations')
    result=quality.scan(database,captured_at='2026-10-21T21:00:00+08:00',as_of='2026-10-22T18:00:00+08:00')
    assert not result['complete']
    assert next(c for c in result['checks'] if c['rule']=='reservation_balance')['status']=='unavailable'


@pytest.mark.parametrize('problem',['wal','digest','future','no_timezone'])
def test_inconsistent_source_refused(tmp_path,problem):
    database=fixture(tmp_path)
    kwargs=dict(captured_at='2026-10-21T21:00:00+08:00',as_of='2026-10-22T18:00:00+08:00')
    if problem=='wal':Path(str(database)+'-wal').write_bytes(b'not a consistent snapshot')
    if problem=='digest':kwargs['expected_sha256']='0'*64
    if problem=='future':kwargs['captured_at']='2026-10-23T00:00:00+08:00'
    if problem=='no_timezone':kwargs['captured_at']='2026-10-21T21:00:00'
    with pytest.raises(ValueError):quality.scan(database,**kwargs)
