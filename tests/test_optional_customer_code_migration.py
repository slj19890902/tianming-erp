"""Constraint-only upgrade preserves referenced products and rejects lossy rollback."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
PARENT = 'ez1022hc'
TARGET = 'fa1023oc'
IDENTITY = 'uq_products_customer_material_code_name'


def config(monkeypatch, database):
    if os.getenv('ERP_UAT_ROOT'):
        from app.core.uat_isolation import prepare_uat_layout
        # Bind this synthetic DB and all writers to a new nested disposable root;
        # retain the outer runner's protected/forbidden paths and storage guards.
        layout = prepare_uat_layout(root=database.parent, database=database,
            port=18181, layout_source=Path(os.environ['ERP_TWIN_LAYOUT_RUNTIME_PATH']),
            protected_paths=[Path(p) for p in json.loads(os.getenv('ERP_UAT_PROTECTED_PATHS_JSON', '[]'))],
            forbidden_roots=[Path(p) for p in json.loads(os.getenv('ERP_UAT_FORBIDDEN_ROOTS_JSON', '[]'))])
        for key, value in layout['environment'].items():
            monkeypatch.setenv(key, value)
        for key, value in {'ERP_UAT_ROOT':layout['root'],
                           'ERP_UAT_ISOLATION_ID':layout['isolation_id'],
                           'ERP_SESSION_COOKIE_NAME':layout['cookie_name'],
                           'ERP_PORT':'18181'}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv('ERP_SECRET_KEY', raising=False)
    else:
        monkeypatch.setenv('ERP_DATABASE_PATH', str(database))
        monkeypatch.setenv('ERP_BACKUP_DIR', str(database.parent / 'backups'))
        monkeypatch.setenv('ERP_SECRET_KEY', 'optional-customer-code-migration-test')
    c = Config(str(ROOT / 'alembic.ini'))
    c.set_main_option('script_location', str(ROOT / 'alembic'))
    c.cmd_opts = type('Options', (), {'x': [f'expected_database_path={database}']})()
    return c


def parent_database(path):
    with sqlite3.connect(path) as c:
        c.executescript('''
CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES('ez1022hc');
CREATE TABLE customers(id INTEGER PRIMARY KEY);
INSERT INTO customers VALUES(1);
CREATE TABLE products (
 id INTEGER PRIMARY KEY,
 customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
 product_code TEXT NOT NULL,
 customer_material_code TEXT NOT NULL,
 product_name TEXT NOT NULL,
 note TEXT DEFAULT 'original',
 CONSTRAINT uq_products_customer_material_code_name UNIQUE (customer_id, customer_material_code, product_name),
 CONSTRAINT uq_products_customer_product_code_name UNIQUE (customer_id, product_code, product_name),
 CHECK(length(product_code)>0));
INSERT INTO products VALUES(1,1,'INTERNAL-1','000123-X','same name','frozen');
CREATE TABLE cascade_child(id INTEGER PRIMARY KEY,product_id INTEGER REFERENCES products(id) ON DELETE CASCADE);
CREATE TABLE restrict_child(id INTEGER PRIMARY KEY,product_id INTEGER REFERENCES products(id) ON DELETE RESTRICT);
INSERT INTO cascade_child VALUES(1,1);
INSERT INTO restrict_child VALUES(1,1);
CREATE INDEX extra_products_note ON products(note) WHERE note IS NOT NULL;
CREATE TRIGGER products_protect BEFORE UPDATE OF note ON products BEGIN SELECT RAISE(ABORT,'frozen note'); END;
CREATE TRIGGER external_product_reference BEFORE INSERT ON restrict_child BEGIN SELECT id FROM products WHERE id=NEW.product_id; END;
CREATE VIEW product_names AS SELECT id,product_name FROM products;
''')


def snapshot(path):
    with sqlite3.connect(path) as c:
        tables = [x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
        return {
            'facts': {t: c.execute('SELECT * FROM "'+t+'" ORDER BY rowid').fetchall() for t in tables},
            'columns': {t: c.execute('PRAGMA table_info("'+t+'")').fetchall() for t in tables},
            'foreign_keys': {t: c.execute('PRAGMA foreign_key_list("'+t+'")').fetchall() for t in tables},
            'objects': c.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('index','trigger','view') AND sql IS NOT NULL AND name<>? ORDER BY type,name", (IDENTITY,)).fetchall(),
        }


def test_constraint_upgrade_roundtrip_preserves_all_references_and_protection(tmp_path, monkeypatch):
    path = tmp_path / 'identity.sqlite3'
    parent_database(path)
    cfg = config(monkeypatch, path)
    assert ScriptDirectory.from_config(cfg).get_heads() == [TARGET]
    before = snapshot(path)
    for action, revision in [('upgrade', TARGET), ('downgrade', PARENT), ('upgrade', TARGET)]:
        getattr(command, action)(cfg, revision)
        assert snapshot(path) == before
        with sqlite3.connect(path) as c:
            assert c.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
            assert c.execute('SELECT * FROM product_names').fetchall() == [(1, 'same name')]
            assert c.execute('SELECT version_num FROM alembic_version').fetchone() == (revision,)
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA foreign_keys=ON')
        for sql in ["UPDATE products SET note='changed' WHERE id=1", 'DELETE FROM products WHERE id=1', 'INSERT INTO restrict_child VALUES(2,999)']:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
        assert c.execute('SELECT * FROM cascade_child').fetchall() == [(1,1)]


def test_new_blank_identity_blocks_downgrade_before_any_change(tmp_path, monkeypatch):
    path = tmp_path / 'blocked.sqlite3'
    parent_database(path)
    cfg = config(monkeypatch, path)
    command.upgrade(cfg, TARGET)
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO products VALUES(2,1,'INTERNAL-2','','same blank name','original')")
        c.execute("INSERT INTO products VALUES(3,1,'INTERNAL-3','','same blank name','original')")
        for sql in ["INSERT INTO products VALUES(4,1,'INTERNAL-4','000123-X','same name','original')", "INSERT INTO products VALUES(4,1,'INTERNAL-2','','same blank name','original')"]:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match='禁止有损降级'):
        command.downgrade(cfg, PARENT)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('fault', ['ALTER TABLE', 'CREATE INDEX extra_products_note'])
def test_ddl_failure_rolls_back_table_children_and_triggers(tmp_path, monkeypatch, fault):
    path = tmp_path / 'atomic.sqlite3'
    parent_database(path)
    cfg = config(monkeypatch, path)
    before = snapshot(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    def fail(conn, cursor, statement, *args):
        if statement.startswith(fault):
            raise RuntimeError('synthetic DDL failure')
    event.listen(Engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic DDL failure'):
            command.upgrade(cfg, TARGET)
    finally:
        event.remove(Engine, 'before_cursor_execute', fail)
    assert snapshot(path) == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT version_num FROM alembic_version').fetchone() == (PARENT,)
    command.upgrade(cfg, TARGET)
    assert snapshot(path) == before


def test_live_style_fk_enforcement_is_not_disabled_by_migration(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine
    path = tmp_path / 'fk-on.sqlite3'
    parent_database(path)
    spec = importlib.util.spec_from_file_location('optional_migration_guard', ROOT / 'alembic/versions/fa1023oc_optional_customer_material_code.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    engine = create_engine('sqlite:///'+str(path))
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        module.op = Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError, match='dedicated FK-off'):
            module.upgrade()
        assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    engine.dispose()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
