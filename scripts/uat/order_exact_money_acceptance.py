"""Run only inside run_task's owned copy; preserve source and frozen facts."""
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
import threading
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError, URLError
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.core.uat_isolation import validate_uat_environment
from desktop_assistant.migration import facts, schema


def run():
    validate_uat_environment()
    database = Path(os.environ['ERP_DATABASE_PATH']).resolve()
    job = Path(os.environ['ERP_UAT_ROOT']).parent
    environment = os.environ.copy()
    def alembic(*args, check=True):
        result = subprocess.run([sys.executable, '-m', 'alembic', '-x',
            'expected_database_path=' + str(database), *args], cwd=ROOT, env=environment,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
        if check and result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        return result
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [('ev1020hr',)]
    before = facts(database)
    old_schema = schema(database)
    alembic('upgrade', 'ew1020em')
    assert facts(database, before['columns']) == before
    # Use the standard nonce/PID-owned server launch, with both service egress
    # guards. The driver only queries this isolated localhost test port.
    from app.core.uat_isolation import spawn_uat_process, uat_server_command, cleanup_owned_launch
    from desktop_assistant.home_process import command
    python = Path(sys.executable)
    port = int(os.environ['ERP_PORT'])
    nonce = uuid.uuid4().hex
    root = Path(os.environ['ERP_UAT_ROOT'])
    lease = Path(os.environ['ERP_UAT_LEASE_PATH'])
    pid = Path(os.environ['ERP_UAT_PID_PATH'])
    attestation = Path(os.environ['ERP_UAT_ATTESTATION_PATH'])
    overrides = {key:value for key,value in os.environ.items() if key.startswith('ERP_')}
    argv = command(uat_server_command(python, ROOT, port), overrides)
    ownership = spawn_uat_process(python=python, project_root=ROOT, port=port,
        stdout_path=Path(os.environ['ERP_UAT_STDOUT_PATH']),
        stderr_path=Path(os.environ['ERP_UAT_STDERR_PATH']), nonce=nonce,
        lease_path=lease, pid_path=pid, attestation_path=attestation,
        overrides=overrides, argv=argv, git='/usr/bin/git')
    opener = build_opener(ProxyHandler({}))
    def status(path):
        try:
            with opener.open('http://127.0.0.1:' + str(port) + path, timeout=2) as response:
                return response.status
        except HTTPError as error:
            return error.code
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if status('/api/health') == 200: break
            except (URLError, TimeoutError): pass
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned isolated app did not become healthy within 60 seconds')
            time.sleep(.2)
        assert status('/frontend-v2/') == 200
        assert status('/api/orders') == 401
    finally:
        # spawn returns ownership metadata rather than its Popen handle. Reap
        # this child after graceful shutdown so a zombie PID cannot be mistaken
        # for a still-running server by the existing ownership cleanup guard.
        def reap():
            try:
                os.waitpid(ownership['pid'], 0)
            except ChildProcessError:
                pass
        reaper = threading.Thread(target=reap, daemon=True)
        reaper.start()
        cleanup_owned_launch(lease_path=lease, pid_path=pid, attestation_path=attestation,
                            nonce=nonce, terminate=True)
        reaper.join(timeout=15)
        assert not reaper.is_alive(), 'owned UAT child has not exited'
    assert facts(database, before['columns']) == before
    alembic('downgrade', 'ev1020hr')
    assert facts(database, before['columns']) == before and schema(database) == old_schema
    alembic('upgrade', 'ew1020em')
    assert facts(database, before['columns']) == before
    with sqlite3.connect(database) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        old_objects = [tuple(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('trigger','index') AND sql IS NOT NULL ORDER BY type,name")]
        mismatches=[]
        for item_id, order_id, quantity, unit_price, subtotal in db.execute('SELECT id,order_id,quantity,unit_price,subtotal FROM sales_order_items'):
            exact = Decimal(str(quantity)) * Decimal(str(unit_price))
            if exact != Decimal(str(subtotal)):
                mismatches.append(dict(item_id=item_id,order_id=order_id,quantity=quantity,
                    unit_price=str(unit_price),frozen_subtotal=str(subtotal),exact_product=str(exact)))
        # Only a companion is written in this disposable copy to prove no-loss refusal.
        db.execute("UPDATE sales_order_items SET subtotal_exact='6.675' WHERE id=(SELECT min(id) FROM sales_order_items)")
        db.commit()
    fingerprint = hashlib.sha256(database.read_bytes()).hexdigest()
    refused = alembic('downgrade', 'ev1020hr', check=False)
    assert refused.returncode != 0 and '禁止丢失金额降级' in refused.stderr
    assert hashlib.sha256(database.read_bytes()).hexdigest() == fingerprint
    result=dict(status='isolated_migration_roundtrip_and_loss_guard_passed',
        old_tables_preserved=len(before['columns']), original_schema_roundtrip=True,
        original_indexes_and_triggers_retained=len(old_objects), integrity='ok', foreign_keys=0,
        isolated_app_startup=True, background_jobs_disabled=True, outbound_network_disabled=True,
        historical_amount_differences=mismatches,
        historical_amounts_modified=False, formal_data_written=False,
        source_is_retained_snapshot=True, latest_formal_acceptance=False)
    (job/'exact-money-migration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='historical_amount_differences'},ensure_ascii=False))


if __name__ == '__main__':
    run()
