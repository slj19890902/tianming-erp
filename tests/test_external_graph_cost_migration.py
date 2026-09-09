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


def migration(filename):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions' / filename
    spec = importlib.util.spec_from_file_location(filename, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sources_exclusive_fk_and_old_paper_rows_survive_roundtrip(tmp_path):
    old = migration('rx10v8x9z72_graph_delivery_cost.py')
    new = migration('rz12v8x9z74_external_graph_delivery_cost.py')
    engine = create_engine(f"sqlite:///{tmp_path / 'constraints.sqlite3'}")
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('sales_delivery_items', 'delivery_inventory_allocations', 'unordered_finished_delivery_allocations',
                      'inventory_lots', 'inventory_movements', 'users', 'purchase_receipt_facts',
                      'incoming_receipt_purpose_allocations', 'external_packaging_receipt_items'):
            db.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            db.exec_driver_sql(f'INSERT INTO {table} VALUES (1)')
        with Operations.context(MigrationContext.configure(db)):
            old.upgrade()
            db.exec_driver_sql("INSERT INTO finance_delivery_graph_cost_facts (id,delivery_item_id,delivery_inventory_allocation_id,inventory_lot_id,consume_movement_id,snapshot_version,source_quantity,source_offset,consumed_quantity,total_cost,currency,source_fingerprint) VALUES (1,1,1,1,1,1,10,0,1,1,'CNY',?)", ('a'*64,))
            db.exec_driver_sql('INSERT INTO finance_delivery_graph_cost_portions (id,fact_id,ordinal,purchase_receipt_fact_id,purpose_allocation_id,full_output_cost,charged_cost,tax_included,tax_rate) VALUES (1,1,0,1,1,10,1,1,0.13)')
            before = db.exec_driver_sql('SELECT * FROM finance_delivery_graph_cost_portions').fetchall()
            for _ in range(2):
                new.upgrade()
                assert db.exec_driver_sql('SELECT external_receipt_item_id FROM finance_delivery_graph_cost_portions').fetchall() == [(None,)]
                new.downgrade()
                assert db.exec_driver_sql('SELECT * FROM finance_delivery_graph_cost_portions').fetchall() == before
            new.upgrade()
            sql = 'INSERT INTO finance_delivery_graph_cost_portions (id,fact_id,ordinal,purchase_receipt_fact_id,purpose_allocation_id,external_receipt_item_id,full_output_cost,charged_cost,tax_included,tax_rate) VALUES (2,1,1,?,?,?,20,2,0,0.13)'
            for values in ((None,None,None), (1,1,1), (1,None,None), (None,1,None), (None,None,999)):
                with pytest.raises(IntegrityError):
                    db.exec_driver_sql(sql, values)
            db.exec_driver_sql(sql, (None,None,1))
            with pytest.raises(RuntimeError, match='已有外购发货成本事实'):
                new.downgrade()
            assert db.exec_driver_sql('SELECT count(*) FROM finance_delivery_graph_cost_portions').scalar() == 2
            assert db.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    engine.dispose()


def test_external_cost_isolated_copy_roundtrip(monkeypatch, tmp_path):
    source = Path(os.environ.get('ERP_SUBKIT_UAT_SOURCE', '')).resolve()
    if source.name != 'source-isolated.sqlite3' or 'tm-uat' not in source.parts:
        pytest.skip('requires explicit paired isolated copy')
    before_hash = hashlib.sha256(source.read_bytes()).digest()
    target = tmp_path / 'external-graph-cost.sqlite3'
    backup = tmp_path / 'before.sqlite3'
    shutil.copy2(source, target)
    shutil.copy2(target, backup)
    assert hashlib.sha256(backup.read_bytes()).digest() == before_hash
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                  for t in ('sales_orders','sales_order_items','inventory_lots','warehouse_locations')}
    config = _config(monkeypatch, target)
    assert ScriptDirectory.from_config(config).get_heads() == ['sb14v8x9z76']
    command.upgrade(config, 'rz12v8x9z74')
    command.downgrade(config, 'ry11v8x9z73')
    command.upgrade(config, 'rz12v8x9z74')
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for table, count in counts.items():
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == count
    assert hashlib.sha256(source.read_bytes()).digest() == before_hash
