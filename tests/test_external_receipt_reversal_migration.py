import hashlib
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
from tests.test_external_graph_cost_migration import migration
from tests.test_p1_131_material_cost_lineage_migration import _config


def test_reversal_fk_uniqueness_and_nonempty_downgrade(tmp_path):
    module = migration('sa13v8x9z75_external_receipt_reversal.py')
    engine = create_engine(f"sqlite:///{tmp_path / 'reversal-constraints.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        for t in ('external_packaging_receipts','users'):
            db.exec_driver_sql(f'CREATE TABLE {t} (id INTEGER PRIMARY KEY)')
            db.exec_driver_sql(f'INSERT INTO {t} VALUES (1),(2)')
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            sql = "INSERT INTO external_packaging_receipt_reversals(receipt_id,idempotency_key,request_fingerprint,reason,reversed_by) VALUES (?,? ,? ,?,?)"
            for values in ((999,'key','a'*64,'误收',1),(1,'key','a'*64,'误收',999),(1,'key','a'*64,' ',1)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql,values)
            db.exec_driver_sql(sql,(1,'key','a'*64,'误收',1))
            for values in ((1,'other','b'*64,'重复',1),(2,'key','b'*64,'串用',1)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql,values)
            with pytest.raises(RuntimeError,match='已有外购实收撤销事实'):
                module.downgrade()
            assert db.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    engine.dispose()


def test_paired_copy_reversal_migration_roundtrip(monkeypatch,tmp_path):
    source = Path(os.environ.get('ERP_SUBKIT_UAT_SOURCE','')).resolve()
    if source.name != 'source-isolated.sqlite3' or 'tm-uat' not in source.parts:
        pytest.skip('requires explicit paired isolated source')
    before = hashlib.sha256(source.read_bytes()).digest()
    target,backup = tmp_path/'reversal.sqlite3',tmp_path/'before.sqlite3'
    shutil.copy2(source,target)
    shutil.copy2(target,backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == before
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        counts = {t:db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in
                  ('sales_orders','sales_order_items','inventory_lots','warehouse_locations','external_packaging_receipts')}
    config = _config(monkeypatch,target)
    assert ScriptDirectory.from_config(config).get_heads() == ['sc15v8x9z77']
    command.upgrade(config,'sa13v8x9z75')
    command.downgrade(config,'rz12v8x9z74')
    command.upgrade(config,'sa13v8x9z75')
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for t,count in counts.items():
            assert db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] == count
    assert hashlib.sha256(source.read_bytes()).digest() == before
