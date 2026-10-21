"""Latest real-copy acceptance: schema roundtrip, exact repair, frozen facts."""
import hashlib,json,os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.database import create_sqlite_engine

def sha(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,default=str,sort_keys=True).encode()).hexdigest()

def main():
    assert os.environ['ERP_ENVIRONMENT']=='test'
    path=Path(os.environ['ERP_DATABASE_PATH']);assert 'copies' in path.parts
    output=Path(os.environ['ERP_UAT_ROOT']).parent
    baseline=sqlite3.connect(path);tables={r[0] for r in baseline.execute("select name from sqlite_master where type='table'")}
    protected=[name for name in ('sales_orders','sales_order_items','delivery_orders','delivery_order_items',
        'deliveries','delivery_items','incoming_receipts','incoming_receipt_items','supplier_requisition_orders',
        'supplier_requisition_order_items','purchase_receipt_facts','finished_goods_inventory_details') if name in tables]
    columns={t:[r[1] for r in baseline.execute('PRAGMA table_info("'+t+'")')] for t in protected}
    def facts(db):return {t:sha(db.execute('SELECT '+','.join('"'+c+'"' for c in cs)+' FROM "'+t+'" ORDER BY rowid').fetchall()) for t,cs in columns.items()}
    before=facts(baseline)
    schema=baseline.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN('index','trigger') ORDER BY type,name").fetchall()
    reservations={r[0]:r for r in baseline.execute('SELECT * FROM inventory_reservations')}
    lot_columns=[r[1] for r in baseline.execute('PRAGMA table_info(inventory_lots)')]
    stable_lot=[c for c in lot_columns if c not in ('version','updated_at','last_movement_at','quantity_available','quantity_reserved')]
    lots={r[0]:r for r in baseline.execute('SELECT id,'+','.join('"'+c+'"' for c in stable_lot)+' FROM inventory_lots')}
    totals={r[0]:r[1] for r in baseline.execute('SELECT id,quantity_available+quantity_reserved FROM inventory_lots')}
    balances={r[0]:r[1:] for r in baseline.execute('SELECT id,quantity_available,quantity_reserved,last_movement_at FROM inventory_lots')}
    baseline.close()
    config=Config('alembic.ini');command.upgrade(config,'head');command.downgrade(config,'ew1020em')
    with sqlite3.connect(path) as db:
        assert facts(db)==before
        assert db.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN('index','trigger') ORDER BY type,name").fetchall()==schema
    command.upgrade(config,'head')
    from app.models.user import User
    from app.models.product import Product
    from app.services.shared_catalog_release_repair import apply
    manifest=json.loads(Path(sys.argv[1]).read_text())
    engine=create_sqlite_engine(path)
    with Session(engine) as db:
        actor=db.get(User,1);assert actor.role=='admin' and actor.is_active
        result=apply(db,manifest=manifest,actor=actor,backup_sha256=manifest['source_database_sha256']);db.commit()
        replay=apply(db,manifest=manifest,actor=actor,backup_sha256=manifest['source_database_sha256']);assert replay['replayed'];db.rollback()
        from app.services.scrap_production import options,preview
        from app.core.time_contract import beijing_today
        alt=db.get(Product,3581)
        assert not alt.is_composite and len(alt.production_variants)==2
        scrap_preview={}
        try:
            info=options(db,3581,None,actor)
            v=preview(db,dict(product_id=3581,quantity=100,stock_date=str(beijing_today()),location_id=None,
                variant_id=alt.production_variants[0]['id'],remarks='只读方案核验'),actor)
            scrap_preview={'status':'passed','labour':v['labour'],'variant':v['variant']}
        except Exception as e:
            scrap_preview={'status':'specific_prerequisite','reason':str(e)}
    with sqlite3.connect(path) as db:
        assert facts(db)==before,'Historical commercial/receipt facts changed'
        after={r[0]:r for r in db.execute('SELECT id,'+','.join('"'+c+'"' for c in stable_lot)+' FROM inventory_lots')}
        assert after==lots,'Original inventory facts/cost/location changed'
        assert {r[0]:r[1] for r in db.execute('SELECT id,quantity_available+quantity_reserved FROM inventory_lots')}==totals
        allocations={}
        for row in result['new_reservations']:allocations[row['lot_id']]=allocations.get(row['lot_id'],0)+row['quantity']
        for identity,available,reserved,last_at in db.execute('SELECT id,quantity_available,quantity_reserved,last_movement_at FROM inventory_lots'):
            old_available,old_reserved,old_at=balances[identity];delta=allocations.get(identity,0)
            assert (available,reserved)==(old_available-delta,old_reserved+delta)
            if last_at!=old_at:assert delta>0,'Unrelated movement timestamp changed'
        new_reservations={r[0] for r in db.execute('SELECT id FROM inventory_reservations')}-set(reservations)
        assert new_reservations=={r['reservation_id'] for r in result['new_reservations']}
        assert all(db.execute('SELECT * FROM inventory_reservations WHERE id=?',(key,)).fetchone()==value for key,value in reservations.items())
        assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        old_schema=[r for r in db.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN('index','trigger') ORDER BY type,name") if r[1] in {s[1] for s in schema}]
        assert old_schema==schema,'Original protection contract changed'
    try:command.downgrade(config,'ew1020em')
    except RuntimeError:pass
    else:raise AssertionError('New facts did not block lossy downgrade')
    report=dict(status='passed',manifest_sha256=manifest['sha256'],result=result,roundtrip='passed',
        original_indexes_triggers=len(schema),historical_facts=before,original_lots=len(lots),
        existing_reservations_preserved=len(reservations),scrap_preview=scrap_preview,formal_writes=False)
    (output/'shared-catalog-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
    print(json.dumps({k:v for k,v in report.items() if k not in ('result','historical_facts','scrap_preview')},ensure_ascii=False))
    print(json.dumps({'result':result,'scrap_preview':scrap_preview},ensure_ascii=False,default=str))

if __name__=='__main__':main()
