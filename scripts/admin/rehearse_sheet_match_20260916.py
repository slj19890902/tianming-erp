"""Readonly formal snapshot -> isolated migration/data-correction rehearsal only."""
from pathlib import Path
import os, sys, sqlite3, json, hashlib, subprocess, time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SOURCE = Path('D:/TianmingERP/shared/data/carton_erp.sqlite3')
TARGET = ROOT / 'data/audit/sheet-match-448-rehearsal.sqlite3'
assert ROOT.name == 'erp-bidirectional-match-20260916'
assert TARGET.resolve() != SOURCE.resolve() and not TARGET.exists()
TARGET.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(TARGET) as dst:
    src.execute('PRAGMA query_only=ON')
    assert src.execute('SELECT version_num FROM alembic_version').fetchall() == [('cc0915',)]
    src.backup(dst)


def facts():
    with sqlite3.connect(TARGET) as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        result = {}
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version' ORDER BY name"):
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")') if r[1] != 'cut_plan_json']
            fields = ','.join('"'+c+'"' for c in columns)
            digest = hashlib.sha256()
            count = 0
            for row in db.execute(f'SELECT {fields} FROM "{table}" ORDER BY {fields}'):
                digest.update(repr(row).encode()); count += 1
            result[table] = (count, digest.hexdigest())
        return result


before = facts()
env = dict(os.environ, ERP_DATABASE_PATH=str(TARGET), ERP_ENVIRONMENT='test', ERP_ALLOWED_ORIGINS='http://127.0.0.1:8000')
def migrate(direction, revision):
    subprocess.run([sys.executable,'-m','alembic','-x',f'expected_database_path={TARGET}',direction,revision],cwd=ROOT,env=env,check=True)
for direction, revision in [('upgrade','sc0916'),('downgrade','cc0915'),('upgrade','sc0916')]:
    migrate(direction, revision)
    assert facts() == before, 'Migration changed historical rows'

os.environ.update(env)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from scripts.admin.sheet_match_20260916 import apply_approved_facts
engine = create_engine('sqlite:///'+TARGET.as_posix())
with Session(engine) as db:
    result = apply_approved_facts(db)
    db.commit()
    replay = apply_approved_facts(db)
    assert replay['replayed'] and replay['audit_id'] == result['audit_id']
after = facts()
changed = [table for table in before if before[table] != after[table]]
assert set(changed) <= {'products','warehouse_goods_profiles','inventory_lots','semi_finished_lot_allowed_products','operation_logs'}, changed
from app.models.warehouse_inventory import InventoryLot
from app.services.material_candidates import candidate_items
from app.services.semi_finished_inventory import semi_finished_candidates_for_product
with Session(engine) as db:
    started = time.monotonic()
    reverse = candidate_items(db, db.get(InventoryLot, 871))
    confirmed = [r['product_id'] for r in reverse if r.get('selectable')]
    assert set(confirmed) == {203,404}, confirmed
    rows = semi_finished_candidates_for_product(db, product_id=38, customer_id=5,
        board_length_mm=1000, board_width_mm=200, material_code='CCC', flute_type='B',
        component_type='whole', pieces_per_box=1, stock_yield_per_sheet=1)
    target = next(r for r in rows if r.lot.id == 832)
    assert target.cut_plan['yield_factor'] == 2 and target.deductible_requirement_quantity == 12
    assert not any(r.lot.id == 871 and r.selectable for r in rows)
    assert not any(r['product_id'] == 351 for r in candidate_items(db, db.get(InventoryLot,829)))
    timing = time.monotonic() - started
engine.dispose()
report = dict(migration_roundtrip='passed', integrity='ok', foreign_key_violations=0,
    historical_rows_unchanged=True, correction=result, idempotency=replay,
    changed_tables=changed, samples={'871':confirmed,'832_yield':2,'829_wrong_product_excluded':True},
    sample_seconds=round(timing,3), isolated_database=str(TARGET))
(TARGET.parent/'sheet-match-448-rehearsal.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
