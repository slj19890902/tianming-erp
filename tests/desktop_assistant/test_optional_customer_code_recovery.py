"""Signed recovery evidence and real constraint migration, never a live database."""
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
from tests.test_optional_customer_code_migration import parent_database
from tests.desktop_assistant.test_local_backup_only import local_case, case
from tests.desktop_assistant.test_stopped_update import PASSWORD

recover = partial(recover_empty_migration, _optional_customer_code=True)
REVISION_FILE = Path(__file__).resolve().parents[2] / 'alembic/versions/fa1023oc_optional_customer_material_code.py'


def apply_constraint(database, source, direction):
    """Run the actual revision in a dedicated connection, with its real transaction."""
    import importlib.util
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location('optional_constraint', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine('sqlite:///' + str(database))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                getattr(module, direction)()
            target = module.revision if direction == 'upgrade' else module.down_revision
            connection.exec_driver_sql('UPDATE alembic_version SET version_num=?', (target,))
    finally:
        engine.dispose()


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / 'instance'
    shared = root / 'shared'
    for path in (shared / 'data', root / 'packages', root / 'control', root / 'staging/migration-fixture'):
        path.mkdir(parents=True)
    database = shared / 'data/carton_erp.sqlite3'
    parent_database(database)
    with sqlite3.connect(database) as db:
        for table in ('users', 'sales_orders'):
            db.execute('CREATE TABLE ' + table + '(id INTEGER PRIMARY KEY)')
    # The deployed parent has already been rebuilt by prior signed migrations;
    # obtain that same quoted-table form with the actual safe round trip.
    apply_constraint(database, REVISION_FILE, 'upgrade')
    apply_constraint(database, REVISION_FILE, 'downgrade')
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
    pack_tree(source, old_package, dict(type='tianming.release.v1', version='old', revision='ez1022hc'), key)
    old = sha(old_package)
    old_package.rename(root / 'packages' / (old + '.zip'))
    pack_tree(source, new_package, dict(type='tianming.release.v1', version='new', revision='fa1023oc',
        reader_capabilities={'shared_product_master_v1': 1}, migration=dict(policy='preserve_existing_facts_v1',
        from_revision='ez1022hc', rollback_package_sha256=old)), key)
    new = sha(new_package)
    new_package.rename(root / 'packages' / (new + '.zip'))
    manifest = signed_release_manifest(root / 'packages' / (new + '.zip'), public)
    report_path = root / 'staging/migration-fixture/report.json'
    write_json(report_path, dict(status='passed', package_sha256=new, source_revision='ez1022hc',
        target_revision='fa1023oc', source_schema=source_schema, result_schema=migration.schema(database),
        source_facts=before, schema_fingerprint_version=2))
    backup = root / 'backup.tmbackup'
    backup.write_bytes(b'already-authenticated-synthetic-backup')
    state = dict(operation='update', current=new, update_backup=str(backup), migration_backup_sha256=sha(backup),
        migration_report=str(report_path), migration_report_sha256=sha(report_path), migration_files=migration.files(shared))
    write_json(root / 'state.json', state)
    driver = tmp_path / 'downgrade.py'
    driver.write_text('import sys\n'
        + 'sys.path.insert(0, ' + repr(str(REVISION_FILE.parents[2])) + ')\n'
        + 'from tests.desktop_assistant.test_optional_customer_code_recovery import apply_constraint\n'
        + "assert sys.argv[-2:]==['downgrade','ez1022hc']\n"
        + 'apply_constraint(' + repr(str(database)) + ', ' + repr(str(source / REVISION_FILE.name)) + ", 'downgrade')\n")
    calls = []
    def prepare(*args, **kwargs):
        calls.append(True)
        return [sys.executable, str(driver), 'upgrade', 'fa1023oc'], {}, False
    monkeypatch.setattr(migration, 'prepare_migration', prepare)
    manager = SimpleNamespace(root=root, public_key=public, _process=lambda: None,
        _execution_release=lambda *args, **kwargs: source)
    return SimpleNamespace(**locals())


def test_unchanged_first_start_failure_restores_exact_source_without_replacing_database(scene):
    c = scene
    result = recover(c.manager, c.new, c.old, c.manifest, c.state)
    assert result['status'] == 'completed' and result['database_replaced'] is False
    assert migration.schema(c.database) == c.source_schema
    assert migration.facts(c.database) == c.before
    assert c.state['schema_authority'] is None
    assert c.state['operation'] == 'migration_running'
    assert c.calls == [True]


@pytest.mark.parametrize('fault', ['new_blank_identity', 'changed_code', 'changed_schema', 'changed_original',
    'bad_report', 'bad_backup', 'active_migration', 'running_candidate'])
def test_changed_data_or_unproven_recovery_never_runs_downgrade(scene, fault):
    c = scene
    if fault == 'new_blank_identity':
        with sqlite3.connect(c.database) as db:
            db.execute("INSERT INTO products VALUES(2,1,'INTERNAL-2','','same blank','original')")
            db.execute("INSERT INTO products VALUES(3,1,'INTERNAL-3','','same blank','original')")
    if fault == 'changed_code':
        with sqlite3.connect(c.database) as db:
            db.execute("UPDATE products SET customer_material_code='' WHERE id=1")
    if fault == 'changed_schema':
        with sqlite3.connect(c.database) as db:
            db.execute('CREATE TABLE unknown(id INTEGER)')
    if fault == 'changed_original':
        (c.shared / 'data/original.txt').write_text('changed original')
    if fault == 'bad_report':
        c.report_path.write_bytes(b'{}')
    if fault == 'bad_backup':
        c.backup.write_bytes(b'changed')
    if fault == 'active_migration':
        c.state['operation'] = 'migration_running'
    if fault == 'running_candidate':
        c.manager._process = lambda: ('running', {})
    before = sha(c.database)
    with pytest.raises(ValueError):
        recover(c.manager, c.new, c.old, c.manifest, c.state)
    assert not c.calls and sha(c.database) == before


@pytest.mark.parametrize('failure', ['none', 'before_write', 'after_write', 'interrupted', 'interrupted_start_failure'])
def test_manager_closes_old_writer_gate_before_start_and_preserves_new_facts(local_case, monkeypatch, failure):
    c = local_case
    manager = c.manager
    database = manager.root / 'shared/data/carton_erp.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('DROP TABLE products')
        db.execute('''CREATE TABLE "products"(id INTEGER PRIMARY KEY, note TEXT,
            customer_id INTEGER NOT NULL, product_code TEXT NOT NULL,
            customer_material_code TEXT NOT NULL, product_name TEXT NOT NULL,
            CONSTRAINT uq_products_customer_material_code_name UNIQUE(customer_id,customer_material_code,product_name))''')
        db.execute("INSERT INTO products VALUES(1,'synthetic',1,'INTERNAL-1','000123','box')")
        db.execute("UPDATE alembic_version SET version_num='ez1022hc'")
    apply_constraint(database, REVISION_FILE, 'upgrade')
    apply_constraint(database, REVISION_FILE, 'downgrade')
    old_package = c.root / 'parent-signed.zip'
    pack_tree(c.root / 'source-one', old_package, dict(type='tianming.release.v1', version='parent',
        revision='ez1022hc', reader_capabilities={'shared_product_master_v1': 1}), c.key)
    manager.stage_release(old_package)
    old = sha(old_package)
    state = manager.state
    state.update(current=old, manual_stop=False, operation='update')
    write_json(manager.root / 'state.json', state)
    c.live['running'] = True
    package = c.root / 'optional-signed.zip'
    from desktop_assistant import local_backup_only as local
    pack_tree(c.root / 'source-local-two', package, dict(type='tianming.release.v1', version='optional',
        revision='fa1023oc', reader_capabilities={'shared_product_master_v1': 1},
        runtime_capabilities={local.CAPABILITY: 1}, migration=dict(policy='preserve_existing_facts_v1',
        from_revision='ez1022hc', rollback_package_sha256=old)), c.key)
    driver = c.root / 'constraint-child.py'
    driver.write_text('import sys\n' + 'sys.path.insert(0, ' + repr(str(REVISION_FILE.parents[2])) + ')\n'
        + 'from tests.desktop_assistant.test_optional_customer_code_recovery import apply_constraint\n'
        + "assert sys.argv[-2:]==['downgrade','ez1022hc']\n"
        + 'apply_constraint(' + repr(str(database)) + ', ' + repr(str(REVISION_FILE)) + ", 'downgrade')\n")
    monkeypatch.setattr(migration, 'prepare_migration', lambda *args, **kwargs:
        ([sys.executable, str(driver), 'upgrade', 'fa1023oc'], {}, False))
    def migrate(release, shared, revision, log, **kwargs):
        assert revision == 'fa1023oc'
        apply_constraint(shared / 'data/carton_erp.sqlite3', REVISION_FILE, 'upgrade')
        log.write_text('synthetic migration child complete')
        if failure.startswith('interrupted') and shared == manager.root / 'shared':
            raise ValueError('synthetic interruption after committed migration')
    monkeypatch.setattr(migration, 'run_migration', migrate)
    original_start = manager.start
    def start():
        if manager.state['current'] == sha(package):
            assert manager.state['previous'] is None and manager.state['schema_authority'] is None
            assert not manager.compatible(old, 'fa1023oc')
            if failure == 'after_write':
                with sqlite3.connect(database) as db:
                    db.execute("INSERT INTO products VALUES(2,'new',1,'INTERNAL-2','','blank')")
                    db.execute("INSERT INTO products VALUES(3,'new',1,'INTERNAL-3','','blank')")
            if failure in ('before_write', 'after_write', 'interrupted_start_failure'):
                raise ValueError('synthetic first-start failure')
        return original_start()
    monkeypatch.setattr(manager, 'start', start)
    before = migration.facts(database)
    with manager.lock():
        if failure == 'none':
            assert manager._update_locked(package, PASSWORD, c.nas) == 'optional'
        else:
            with pytest.raises(ValueError):
                manager._update_locked(package, PASSWORD, c.nas)
    if failure.startswith('interrupted'):
        assert not c.live['running'] and manager.state['operation'] == 'migration_failed'
        # This shared fixture uses inert Windows files; process admission is
        # synthetic here. The release gate separately runs actual Mac binaries.
        monkeypatch.setattr(manager, '_runtime_python', lambda *args, **kwargs: Path(sys.executable))
        if failure == 'interrupted':
            assert '恢复运行' in manager.recover_interrupted_update()
        else:
            with pytest.raises(ValueError, match='旧程序不支持'):
                manager.recover_interrupted_update()
            assert not c.live['running']
        assert manager.state['current'] == sha(package)
        assert manager.state['previous'] is None and manager.state['schema_authority'] is None
        assert not manager.compatible(old, 'fa1023oc')
        assert migration.facts(database) == before
        return
    if failure == 'before_write':
        assert manager.state['current'] == old and c.live['running']
        assert manager.state['operation'] == 'update_recovered'
        assert migration.facts(database) == before
        assert manager.state['unchanged_customer_code_recovery']['status'] == 'completed'
    elif failure == 'after_write':
        assert not c.live['running']
        assert manager.state['operation'] == 'update_failed_reader_contract'
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT count(*) FROM products WHERE customer_material_code=''").fetchone() == (2,)
            assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('fa1023oc',)
    else:
        assert c.live['running'] and manager.state['current'] == sha(package)
        assert manager.state['previous'] is None and manager.state['schema_authority'] is None
        assert not manager.compatible(old, 'fa1023oc')
