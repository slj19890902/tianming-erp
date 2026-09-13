"""Reviewed 204/205 master-only transition. Caller owns backup and stop gates.

The CLI creates a NEW isolated SQLite copy and rehearses there only.
Formal application is callable only from the audited managed release hook.
"""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from app.models.product import Product
from app.models.user import User
from app.models.order import OrderItem
from app.services.multilevel_bom_compile import compile_master_order_bom
from app.services.multilevel_bom_plan import plan_bom
from app.services.multilevel_bom_master_transition import transition_master_boms
from app.services.multilevel_bom_master import preview_master_structure

EXPECTED = {3494:6,3771:7,3772:10,3773:4,3783:5,3799:13}
ALLOWED = {'products','product_bom_components','product_bom_profiles',
           'product_bom_inventory_relations','operation_logs','master_data_object_versions','sqlite_sequence'}

def snapshots(db):
    tables = db.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")).scalars()
    result = {}
    for table in tables:
        if table in ALLOWED:
            continue
        digest = hashlib.sha256()
        rows = db.execute(text('SELECT * FROM "'+table.replace('"','""')+'" ORDER BY rowid'))
        count = 0
        for row in rows:
            digest.update(repr(tuple(row)).encode('utf-8')); count += 1
        result[table] = (count,digest.hexdigest())
    return result

def apply_reviewed(db_path):
    engine = create_engine('sqlite:///'+str(db_path))
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        conn.commit()
        with Session(bind=conn) as db:
            db.execute(text('BEGIN IMMEDIATE'))
            before = snapshots(db)
            unrelated = db.execute(text('SELECT * FROM products WHERE id NOT IN (3494,3799) ORDER BY id')).all()
            old = db.execute(text('SELECT id,parent_product_id,component_product_id,quantity_per_set FROM product_bom_components WHERE parent_product_id IN (3494,3799) ORDER BY id')).all()
            assert [tuple(r) for r in old] == [(12,3494,3771,15),(13,3494,3783,20),(14,3494,3772,6),(26,3799,3771,3),(27,3799,3783,4)], 'Reviewed recipe changed'
            actor = db.scalar(select(User).where(User.role=='admin',User.is_active.is_(True)).order_by(User.id))
            assert actor is not None
            def change(pid,mode,children):
                return {'parent_product_id':pid,'inventory_mode':mode,'components':[
                    {'component_product_id':child,'quantity_per_set':qty,'inventory_relation':relation,
                     'is_required':True,'is_die_cut':False,'display_order':index,
                     'display_mode':'internal_only','show_on_delivery':False}
                    for index,(child,qty,relation) in enumerate(children,1)]}
            result = transition_master_boms(db,customer_id=136,actor=actor,
                expected_versions=EXPECTED,disable_subkits={},changes=[
                    change(3799,'assembled',[(3771,3,'assembly'),(3783,4,'assembly')]),
                    change(3494,'manufactured',[(3799,5,'accompany'),(3772,6,'accompany'),(3773,4,'accompany')])])
            db.flush()
            graph = preview_master_structure(db,3494)
            for pid, expected in [(3494,{3494:1,3799:5,3771:15,3783:20,3772:6,3773:4}),
                                  (3799,{3799:1,3771:3,3783:4})]:
                item = OrderItem(id=0,product_id=pid,quantity=1)
                compiled = compile_master_order_bom(db,item)
                plan = plan_bom(compiled.graph,1)
                assert {p.product_id:p.required_units for p in plan.products} == expected
                assert item not in db, 'Preview must not create an order'
            assert db.get(Product,3799).unit == '套'
            current = db.execute(text('SELECT * FROM products WHERE id NOT IN (3494,3799) ORDER BY id')).all()
            for a,b in zip(unrelated,current):
                if a == b:
                    continue
                changed = {k for k in a._mapping if a._mapping[k]!=b._mapping[k]}
                assert a._mapping['id'] == b._mapping['id'] == 3773
                assert changed <= {'is_internal_component','updated_at'} and b._mapping['is_internal_component'] == 1
            after = snapshots(db)
            assert after == before, ['Changed table: '+table for table in before if before[table] != after.get(table)]
            assert not db.execute(text('PRAGMA foreign_key_check')).all()
            assert db.execute(text('PRAGMA integrity_check')).scalar() == 'ok'
            db.commit()
            return {'master_versions':result['versions'],'graph':graph,'preserved_tables':len(before),
                    'historical_business_rows_unchanged':True,'integrity':'ok','foreign_key_violations':0,
                    'actor_id':actor.id}

if __name__ == '__main__':
    source = Path(r'D:/TianmingERP/shared/data/carton_erp.sqlite3')
    target = Path(sys.argv[1]).resolve()
    assert target != source.resolve() and not target.exists()
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect('file:'+source.as_posix()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print(json.dumps(apply_reviewed(target),ensure_ascii=False,default=str))
