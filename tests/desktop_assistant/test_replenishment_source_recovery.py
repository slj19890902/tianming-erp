"""Actual signed-revision rollback only while no new stock work exists."""
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import shutil
import sqlite3
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from desktop_assistant import migration
from desktop_assistant.exact_sales_reader import recover_empty_migration
from desktop_assistant.storage import pack_tree, sha, write_json, signed_release_manifest
from tests.desktop_assistant.test_optional_customer_code_recovery import apply_constraint

recover = partial(recover_empty_migration, _replenishment_source=True)
REVISION_FILE = Path(__file__).resolve().parents[2] / 'alembic/versions/fb1024rs_replenishment_production_sources.py'


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / 'instance'
    shared = root / 'shared'
    for path in (shared / 'data', root / 'packages', root / 'control', root / 'staging/migration-fixture'):
        path.mkdir(parents=True)
    database = shared / 'data/carton_erp.sqlite3'
    with sqlite3.connect(database) as db:
        for table in ('products', 'users', 'stock_replenishment_order_items', 'inventory_reservations', 'inventory_lots'):
            db.execute('CREATE TABLE "' + table + '"(id INTEGER PRIMARY KEY)')
            db.execute('INSERT INTO "' + table + '" VALUES(1)')
        db.execute('CREATE TABLE "scrap_production_jobs"(id INTEGER PRIMARY KEY, order_item_id INTEGER, status TEXT)')
        db.execute("INSERT INTO scrap_production_jobs VALUES(1,2,'completed')")
        db.execute('CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL)')
        db.execute("INSERT INTO alembic_version VALUES('fa1023oc')")
    (shared / 'data/original.txt').write_text('original attachment')
    before = migration.facts(database)
    source_schema = migration.schema(database)
    apply_constraint(database, REVISION_FILE, 'upgrade')
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    source = tmp_path / 'source'
    source.mkdir()
    shutil.copyfile(REVISION_FILE, source / REVISION_FILE.name)
    old_package, new_package = tmp_path / 'old.zip', tmp_path / 'new.zip'
    pack_tree(source, old_package, dict(type='tianming.release.v1', version='old', revision='fa1023oc'), key)
    old = sha(old_package)
    old_package.rename(root / 'packages' / (old + '.zip'))
    pack_tree(source, new_package, dict(type='tianming.release.v1', version='new', revision='fb1024rs',
        reader_capabilities={'replenishment_production_v1': 1}, migration=dict(policy='preserve_existing_facts_v1',
        from_revision='fa1023oc', rollback_package_sha256=old)), key)
    new = sha(new_package)
    new_package.rename(root / 'packages' / (new + '.zip'))
    manifest = signed_release_manifest(root / 'packages' / (new + '.zip'), public)
    report_path = root / 'staging/migration-fixture/report.json'
    write_json(report_path, dict(status='passed', package_sha256=new, source_revision='fa1023oc',
        target_revision='fb1024rs', source_schema=source_schema, result_schema=migration.schema(database),
        source_facts=before, schema_fingerprint_version=2))
    backup = root / 'backup.tmbackup'
    backup.write_bytes(b'authenticated-synthetic-backup-fixture')
    state = dict(operation='update', current=new, update_backup=str(backup), migration_backup_sha256=sha(backup),
        migration_report=str(report_path), migration_report_sha256=sha(report_path), migration_files=migration.files(shared))
    write_json(root / 'state.json', state)
    driver = tmp_path / 'downgrade.py'
    driver.write_text('import sys\n'
        + 'sys.path.insert(0, ' + repr(str(REVISION_FILE.parents[2])) + ')\n'
        + 'from tests.desktop_assistant.test_optional_customer_code_recovery import apply_constraint\n'
        + "assert sys.argv[-2:]==['downgrade','fa1023oc']\n"
        + 'apply_constraint(' + repr(str(database)) + ', ' + repr(str(source / REVISION_FILE.name)) + ", 'downgrade')\n")
    calls = []
    def prepare(*args, **kwargs):
        calls.append(True)
        return [sys.executable, str(driver), 'upgrade', 'fb1024rs'], {}, False
    monkeypatch.setattr(migration, 'prepare_migration', prepare)
    manager = SimpleNamespace(root=root, public_key=public, _process=lambda: None,
        _execution_release=lambda *args, **kwargs: source)
    return SimpleNamespace(**locals())


def test_actual_empty_downgrade_restores_exact_schema_without_replacing_data(scene):
    c = scene
    result = recover(c.manager, c.new, c.old, c.manifest, c.state)
    assert result['status'] == 'completed' and result['database_replaced'] is False
    assert migration.schema(c.database) == c.source_schema
    assert migration.facts(c.database) == c.before
    assert c.state['schema_authority'] is None
    assert c.calls == [True]


@pytest.mark.parametrize('fault', ['sheet_work', 'scrap_source', 'changed_old_row', 'changed_original',
    'bad_report', 'bad_backup', 'active_migration', 'running_candidate'])
def test_new_facts_or_changed_evidence_never_runs_downgrade(scene, fault):
    c = scene
    with sqlite3.connect(c.database) as db:
        if fault == 'sheet_work':
            db.execute("INSERT INTO stock_replenishment_production_jobs(id,stock_replenishment_item_id,"
                "reservation_id,product_id,snapshot_json,input_quantity,expected_output,actual_output,status,version,actor_id) "
                "VALUES(1,1,1,1,'{}',2,2,0,'cancelled',1,1)")
        if fault == 'scrap_source':
            db.execute("INSERT INTO scrap_production_jobs VALUES(2,NULL,'pending',1)")
        if fault == 'changed_old_row':
            db.execute("UPDATE scrap_production_jobs SET status='cancelled' WHERE id=1")
    if fault == 'changed_original': (c.shared / 'data/original.txt').write_text('changed')
    if fault == 'bad_report': c.report_path.write_bytes(b'{}')
    if fault == 'bad_backup': c.backup.write_bytes(b'changed')
    if fault == 'active_migration': c.state['operation'] = 'migration_running'
    if fault == 'running_candidate': c.manager._process = lambda: ('running', {})
    before = sha(c.database)
    with pytest.raises(ValueError): recover(c.manager, c.new, c.old, c.manifest, c.state)
    assert not c.calls and sha(c.database) == before
