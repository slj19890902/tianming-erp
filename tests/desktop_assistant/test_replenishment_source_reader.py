"""Signed old programs must not silently ignore independent stock work."""
from contextlib import closing
import sqlite3

import pytest

from desktop_assistant import replenishment_source_reader as reader
from desktop_assistant.reader_contract import target_readers
from desktop_assistant.storage import pack_tree, write_json, sha
from tests.desktop_assistant import test_recovery as recovery


def test_existing_scrap_is_not_a_new_source_fact():
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE scrap_production_jobs(id INTEGER, order_item_id INTEGER)')
        db.execute('INSERT INTO scrap_production_jobs VALUES(1,7)')
        assert not reader.has_schema(db) and not reader.has_facts(db)
        db.execute('ALTER TABLE scrap_production_jobs ADD COLUMN stock_replenishment_item_id INTEGER')
        assert reader.has_schema(db) and not reader.has_facts(db)
        db.execute('UPDATE scrap_production_jobs SET stock_replenishment_item_id=8')
        assert reader.has_facts(db)


@pytest.mark.parametrize('status', ['pending', 'completed', 'cancelled'])
def test_every_retained_sheet_job_blocks_lossy_recovery(status):
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE stock_replenishment_production_jobs(id INTEGER, status TEXT)')
        assert reader.has_schema(db) and not reader.has_facts(db)
        db.execute('INSERT INTO stock_replenishment_production_jobs VALUES(1,?)', (status,))
        assert reader.has_facts(db)


@pytest.mark.parametrize('shape', ['sheet', 'scrap'])
def test_target_reader_admission_preserves_schema_and_file(tmp_path, shape):
    database = tmp_path / 'synthetic.sqlite3'
    with sqlite3.connect(database) as db:
        if shape == 'sheet':
            db.execute('CREATE TABLE stock_replenishment_production_jobs(id INTEGER)')
        else:
            db.execute('CREATE TABLE scrap_production_jobs(id INTEGER, stock_replenishment_item_id INTEGER)')
    before = sha(database)
    with pytest.raises(ValueError, match='reader'):
        target_readers(database, {}, {}, None, None, factory=False)
    target_readers(database, {}, {'reader_capabilities': {reader.CAPABILITY: 1}}, None, None, factory=False)
    for invalid in ({}, {reader.CAPABILITY: True}, {reader.CAPABILITY: '1'}):
        with pytest.raises(ValueError, match='独立补库'):
            reader.require_reader(database, {'reader_capabilities': invalid})
    assert reader.require_reader(database, {'reader_capabilities': {reader.CAPABILITY: 1}})
    assert sha(database) == before


def test_signed_reader_blocks_old_binary_even_with_schema_authority():
    fixture = recovery.RecoveryTests()
    fixture.setUp()
    try:
        manager = fixture.manager
        old = manager.state['current']
        fixture.release('stock-work', 'r1')
        package = fixture.root / 'stock-work-signed.zip'
        pack_tree(fixture.root / 'source-stock-work', package,
                  dict(type='tianming.release.v1', version='stock-work', revision='r1',
                       reader_capabilities={reader.CAPABILITY: 1}), fixture.key)
        current = manager.stage_release(package)['id']
        state = dict(current=current, previous=old, schema_authority=current)
        write_json(manager.root / 'state.json', state)
        with sqlite3.connect(manager.root / 'shared/data/carton_erp.sqlite3') as db:
            db.execute('CREATE TABLE stock_replenishment_production_jobs(id INTEGER)')
        assert not manager.compatible(old, 'r1')
        assert manager.compatible(current, 'r1')
        cached = manager.manifest(old)
        cached['reader_capabilities'] = {reader.CAPABILITY: 1}
        write_json(manager.root / 'releases' / old / 'manifest.json', cached)
        assert not manager.compatible(old, 'r1')
        assert manager.state == state
    finally:
        fixture.tearDown()
