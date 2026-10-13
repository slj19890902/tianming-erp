"""Run only inside scripts/uat/run_task.py; retain evidence outside its disposable copy."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.validation.rehearse_delivery_quantity_migration import original_columns, evidence, digest


def migrate(database, direction, target):
    # Keep every path and the bound session secret supplied by run_task.py.
    # The older rehearsal helper predates managed UAT and replaces these.
    return subprocess.run([sys.executable, '-B', '-m', 'alembic',
        '-x', f'expected_database_path={database}', direction, target], cwd=ROOT,
        env=os.environ.copy(), capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=120)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
    database=Path(os.environ['ERP_DATABASE_PATH']).resolve()
    owned=Path(os.environ['ERP_UAT_ROOT']).resolve()
    assert owned in database.parents and os.environ.get('ERP_UAT_ISOLATION_ID')
    assert os.environ.get('ERP_ENVIRONMENT')=='test'
    output=args.evidence.resolve();output.mkdir(parents=True,exist_ok=True)
    assert output != owned and owned not in output.parents, 'Evidence must survive disposal of the UAT copy'
    columns=original_columns(database);before=evidence(database,columns)
    assert before['revision']==[('ep1010md',)]
    (output/'original-facts.json').write_text(json.dumps(before,ensure_ascii=False,indent=2))
    checkpoints=[]
    for direction,target in [('upgrade','eq1013oi'),('downgrade','ep1010md'),('upgrade','eq1013oi')]:
        # Application-owned paths must remain under the runner's UAT root.
        # Only the diagnostic output is retained outside that disposable tree.
        result=migrate(database,direction,target)
        (output/f'{len(checkpoints)}-{direction}.log').write_text(result.stdout+result.stderr)
        assert result.returncode==0,result.stderr
        after=evidence(database,columns)
        assert after['revision']==[(target,)]
        assert after['tables']==before['tables']
        assert after['schema_objects']==before['schema_objects']
        (output/f'{len(checkpoints)}-facts.json').write_text(json.dumps(after,ensure_ascii=False,indent=2))
        checkpoints.append(dict(direction=direction,revision=target,original_facts_preserved=True))
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT count(*) FROM sales_order_items WHERE stock_identity_snapshot_json IS NOT NULL').fetchone()[0]==0
        db.execute("UPDATE sales_order_items SET stock_identity_snapshot_json='{}' WHERE id=(SELECT min(id) FROM sales_order_items)")
    guard_before=digest(database)
    result=migrate(database,'downgrade','ep1010md')
    (output/'refused-downgrade.log').write_text(result.stdout+result.stderr)
    assert result.returncode!=0 and '禁止有损降级' in result.stderr
    assert digest(database)==guard_before
    report=dict(old_business_tables=len(columns),indexes_and_triggers=len(before['schema_objects']),
        checkpoints=checkpoints,historical_snapshot_backfill=False,integrity_check='ok',foreign_key_check=0,
        new_fact_downgrade_refused_without_writes=True,production_modified=False)
    (output/'migration-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
