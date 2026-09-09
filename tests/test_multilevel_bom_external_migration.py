import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_real_node_foreign_key_and_nonempty_downgrade(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/ry11v8x9z73_bom_external_identity.py'
    spec = importlib.util.spec_from_file_location('bom_external_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'constraints.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('sales_order_item_external_components', 'sales_order_item_bom_components'):
            db.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            db.exec_driver_sql(f'INSERT INTO {table} VALUES (1)')
        db.exec_driver_sql('CREATE TABLE order_bom_graph_products (order_item_id INTEGER,product_id INTEGER, PRIMARY KEY(order_item_id,product_id))')
        db.exec_driver_sql('INSERT INTO order_bom_graph_products VALUES (1,3)')
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
            for values in ('1,1,999,1', '999,1,3,1', '1,1,3,999'):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(f'INSERT INTO order_bom_external_components VALUES ({values})')
            db.exec_driver_sql('INSERT INTO order_bom_external_components VALUES (1,1,3,1)')
            with pytest.raises(RuntimeError, match='已有真实BOM外购关联'):
                migration.downgrade()
            assert db.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    engine.dispose()


def test_isolated_copy_upgrade_downgrade_reupgrade(monkeypatch, tmp_path):
    source = Path(os.environ.get('ERP_SUBKIT_UAT_SOURCE', '')).resolve()
    if source.name != 'source-isolated.sqlite3' or 'tm-uat' not in source.parts:
        pytest.skip('requires explicit isolated copy')
    before_hash = hashlib.sha256(source.read_bytes()).digest()
    database = tmp_path / 'bom-external.sqlite3'
    backup = tmp_path / 'before.sqlite3'
    shutil.copy2(source, database)
    shutil.copy2(database, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == before_hash
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                  for t in ('sales_orders','sales_order_items','inventory_lots','warehouse_locations')}
    config = _config(monkeypatch, database)
    assert ScriptDirectory.from_config(config).get_heads() == ['rz12v8x9z74']
    command.upgrade(config, 'ry11v8x9z73')
    command.downgrade(config, 'rx10v8x9z72')
    command.upgrade(config, 'ry11v8x9z73')
    with sqlite3.connect(database) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for table, count in counts.items():
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == count
        assert db.execute('SELECT count(*) FROM order_bom_external_components').fetchone() == (0,)
    assert hashlib.sha256(source.read_bytes()).digest() == before_hash
